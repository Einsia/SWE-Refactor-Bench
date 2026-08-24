"""The three-stage ladder, in one place.

    stage 1  agentic audit gate    pass/fail.  Fail => 0, and nothing else runs.
    stage 2  behavioural modules        40 points, or none: every scored check across
                                       every weighted module, or the stage pays 0.
    stage 3  verification probe         only above both rungs: stage 1 passed and
                                       stage 2 at full marks.  6 models get an hour
                                       each to write a behavioural test the migrated
                                       repository fails; every model that cannot
                                       earns the submission 10 points, for 60.

So a task's score is one of 0, 40, or 40 plus 10 per surviving adversary -- there is
no partial credit inside stage 2, and no stage 3 without all of stage 2.

Why the gate: stage 3 costs six model-hours per submission, and it asks whether
any difference from State A remains that stage 2 did not find.  Asked of a tree
stage 2 already found differences in, the answer is known before the models start
— the adversaries would be racing to report the same unfinished work the suite
already scored, and the budget buys a confirmation instead of a finding.  So the
question is put only to a submission that has run out of cheap answers: one that
passed every scored check in stage 2, and passed the audit gate before it.
Stage 1 is part of the same reasoning rather than a separate rule -- a submission
whose gate failed scores zero whatever the adversaries find, so its six
model-hours buy a number the scorer is required to discard.  Both rungs are asked
where the rounds are launched, by ``behavioural_reaches_gate`` and
``audit_passes_gate``; each is the same grader answering a second question, so
what may run and what may be paid for cannot drift apart.

The gate and the payment are one event, not two: stage 2 pays in full or not at
all, so "was it paid" and "may stage 3 run" are the same question asked twice.
That is why nothing here compares the stage's score against a threshold -- there
is no figure between 0 and ``behavioural_points`` for a threshold to sit in.

Two things this module is careful about, because both are ways a scorer can lie:

* **A stage that did not run is not a stage that failed.**  A verifier container
  that OOMs has established nothing about the submission.  The verdict records
  ``blocked_by`` and ``harness_error`` so a run like that is visibly different
  from an honest zero, and can be re-run rather than published.
* **An empty pool scores zero, not one.**  A module that produced no checks
  earns nothing.  Rating an empty denominator as success would make deleting a
  module the cheapest way to pass it.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ScoringPolicy
from .result import Check, StageResult, pooled

SCHEMA = "swerefactor.score/1"

#: How many of a reporting module's observations reach summary.txt.  The rest stay
#: in results/behavioural.json, which the last line says so a reader knows to look.
_MAX_OBSERVATIONS = 40
#: Per-observation ceiling.  Long enough for a sentence with a list in it; short
#: enough that one note cannot push the module table off a terminal.
_OBSERVATION_CHARS = 400


def _clip_observation(text: str) -> str:
    """One line, bounded.  Truncates toward the front, which is what gets read.

    A module writes an observation as a headline followed by its evidence, so the
    front is the finding and the tail is the list.  Keeping the tail would keep the
    part that means nothing without the part that was dropped.
    """
    flat = " ".join(str(text).split())
    if len(flat) <= _OBSERVATION_CHARS:
        return flat
    return flat[:_OBSERVATION_CHARS - 3] + "..."


#: Module statuses whose zero is a finding about the submission rather than an
#: absence of one.  Everything else is referred back for a re-run.
#:
#: One name for a distinction ``grade_behavioural`` draws in more than one place --
#: which zeros are a measurement, and which the report has to word as an absence.
#: Two such lists disagreed once already: ``blocking`` tested ``status == "ok"``
#: while ``dead`` tested ``status != "ok"``, so a status added to one side arrived on
#: the other by default, and a module that overran its budget reached ``dead`` --
#: publishing "re-run the stage" for a submission the stage had in fact measured.
#: That is why the set is named once here rather than spelled out at each use.
#:
#: ``timeout`` is here because the budget is the task author's own declaration and
#: the reference tree meets it on the same machine; see the comment on ``blocking``.
#: A signal, an OOM kill, and an exit-zero module that wrote no checks are not, and
#: adding one would need the same argument made about it: that some limit the task
#: declared was exceeded, by this submission, where a correct one would not have
#: exceeded it.
_MEASURED_STATUSES = frozenset({"ok", "timeout"})

#: Module statuses whose zero stands, whether or not it is a measurement.
#:
#: A second name because the first one was answering two questions at once, and
#: ``unreached`` separates them: a module the stage clock never got to is *not* a
#: fact about the submission -- nothing ran, so there is nothing it observed -- but
#: its zero is still charged, because the alternative is worse in both directions.
#: Referring the stage back for a re-run would refuse a score to a run whose other
#: modules did measure it (lang04/max: ten modules with real numbers, discarded to
#: report the eleventh), and dropping the module from the arithmetic would pay a
#: denominator assembled from whoever finished -- so a submission that exhausts the
#: stage clock on module 2 would have modules 3..N cost it nothing at all.  Charged
#: at exactly its own weight in the rate, which is the figure that says how much of
#: the suite was measured; what the ladder pays is decided separately, by whether
#: every weighted module passed every scored check.
#:
#: Every status is charged -- ``error`` included -- so this set decides only how the
#: notes word the zero, not whether a number is published.  It is kept
#: because that wording is the difference between "this module measured your tree
#: against a budget and lost" and "this module died and we cannot say why".
_CHARGED_STATUSES = _MEASURED_STATUSES | frozenset({"unreached"})


def _is_charged(status: str) -> bool:
    return status in _CHARGED_STATUSES


def _reported(mods: Any) -> int:
    """How many of a stage-2 module table's rows are a module that ran.

    The count of rows stopped being that number when the suite began seeding a row
    per declared module before the first one starts, so that a stage killed at its
    timeout still describes its whole suite.  A stage killed before any module
    finished now has a full table of `unreached` rows, and anything asking "did this
    stage measure anything" by length alone would read that as N measurements.

    Rows with no status at all count as reporting: this also reads verdicts written
    before `unreached` existed, where every row in the table was a module that ran.
    """
    if not isinstance(mods, list):
        return 0
    return sum(1 for m in mods
               if not isinstance(m, dict)
               or str(m.get("status", "ok")) != "unreached")


@dataclass
class ModuleScore:
    id: str
    title: str
    weight: float
    rate: float
    passed: int
    failed: int
    errored: int
    skipped: int
    status: str
    #: Did every scored check in this module pass?  The stage is all-or-nothing, so
    #: this is the row's whole contribution to the score and the column a reader
    #: checks: the stage pays in full exactly when every weighted row here is
    #: complete, and one `no` explains a zero beside a table of rates near 1.00.
    #:
    #: Not recoverable from `rate`, which is why it is stored.  A module with no
    #: scoring checks at all rates 0.0 -- see `scored_weight` below -- and so does
    #: a module that ran ten checks and failed all ten; the first is a reporting
    #: module that must not block the stage and the second must.
    complete: bool = False
    #: How many of this module's checks scored -- `pooled`'s denominator, and the
    #: denominator of `rate`.  Every check inside a module counts once, so this is a
    #: COUNT of checks and `passed/scored_weight` is the module's rate exactly.  Kept
    #: under its old name because it is written into every score.json on disk and
    #: read back by `from_dict`; when checks carried individual weights it was their
    #: sum, and with equal weighting the sum of N ones is N.
    #:
    #: Zero means the module scored NOTHING, which is a different fact from a rate of
    #: 0.0 and not recoverable from it: a module whose every check is an observation
    #: is handed rate 0.0 by definition, and so is a module that ran ten real checks
    #: and failed all ten.  `note` is what tells those apart, and `rate` alone cannot.
    scored_weight: float = 0.0
    #: The scorer's own reason for this rate -- why a zero is a zero.
    note: str = ""
    #: What the module said about its own run, carried through from the module's
    #: result JSON.  Kept apart from `note` because they answer different
    #: questions and a module can have both: a module that declined cases as
    #: inapplicable explains that here, while `note` stays free for the scorer.
    notes: list[str] = field(default_factory=list)
    #: Weight-zero observations the module recorded, for a module whose whole job
    #: is to report.  Carried here because the renderer is handed ModuleScores and
    #: never sees the StageResult, so without this a reporting module's entire
    #: output reaches results/behavioural.json and nothing else.  Not scored, and
    #: deliberately not merged with either field above: `note` is the SCORER's
    #: sentence about the module, `notes` are the module's about its own RUN, and
    #: these are the module's about the SUBMISSION.  Three sources, and a reader
    #: acting on one needs to know which it is.
    observations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "weight": round(self.weight, 6),
            "rate": round(self.rate, 6),
            "complete": bool(self.complete),
            "status": self.status,
            "scored_weight": round(self.scored_weight, 6),
            "checks": {
                "passed": self.passed,
                "failed": self.failed,
                "errored": self.errored,
                "skipped": self.skipped,
            },
            "note": self.note,
            "notes": list(self.notes),
            "observations": list(self.observations),
        }


def _unscored_from_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """Read the discarded-stage record under whichever of its three names is there.

    `unscored` and `uncredited` are the same shape, so either can be taken whole.
    `uncounted` is not: it reports `modules` as a count where the other two carry
    the module rows, and `report.py` iterates those rows.  Restoring an int there
    would give a verdict that renders one way when built by the scorer and raises
    `TypeError` when re-read from its own JSON -- so a file carrying only the third
    spelling keeps its count under a name that says it is one, and the module table
    is simply absent, which is honest: that file never stored the rows.
    """
    got = raw.get("unscored") or raw.get("uncredited")
    if got:
        return dict(got)
    got = raw.get("uncounted") or {}
    out: dict[str, Any] = {}
    for stage, entry in got.items():
        if not isinstance(entry, dict):
            continue
        entry = dict(entry)
        mods = entry.pop("modules", None)
        if isinstance(mods, list):
            entry["modules"] = mods
        elif mods is not None:
            entry["module_count"] = mods
        # `measured: False` is how that spelling records a reading it could not
        # take; the other two record it as an `error` key, which is what every
        # consumer of this field branches on.
        if entry.get("measured") is False and "error" not in entry:
            entry["error"] = entry.get("note") or "the stage result could not be read"
        entry.pop("measured", None)
        entry.pop("ran", None)
        if "stage_blocked_by" in entry:
            entry["blocked_by"] = entry.pop("stage_blocked_by")
        entry.setdefault("counted", False)
        out[stage] = entry
    return out


@dataclass
class Verdict:
    """The graded outcome of one submission."""

    task: str
    score: float = 0.0
    max_score: float = 100.0
    audit_gate: str = "not-run"          # pass | fail | not-run
    behavioural_points: float = 0.0
    behavioural_rate: float = 0.0
    verification_points: float = 0.0
    adversaries_total: int = 0
    adversaries_survived: int = 0
    stages_run: list[str] = field(default_factory=list)
    blocked_by: str = ""
    harness_error: str = ""
    modules: list[ModuleScore] = field(default_factory=list)
    #: A stage that RAN but whose result the ladder discarded, summarised for the
    #: reader only.  Never contributes to ``score``: a stage-1 fail is 0 by policy
    #: and that is not up for negotiation here.  It exists because "stage 2 was
    #: never run" and "stage 2 ran, passed everything, and was thrown away" are
    #: different facts about a submission, and the report was printing the first
    #: when the second was true -- which loses the single most useful sentence
    #: about a tree that is behaviourally perfect and structurally rejected.
    unscored_stages: dict[str, str] = field(default_factory=dict)
    audit_failures: list[dict[str, Any]] = field(default_factory=list)
    verification_breaks: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Stages that produced a result the ladder did not count, keyed by stage
    #: name.  A stage above a failed gate is still *run* by anyone driving the
    #: stages by hand, and its result file lands in the same directory as this
    #: verdict -- so reporting it as never executed contradicts an artifact
    #: sitting next to the report.  Each value carries that stage's own numbers.
    #:
    #: Nothing here is part of the score, and that separation is the point:
    #: `behavioural_points` / `behavioural_rate` are what the submission *earned*
    #: and must stay 0 when the gate failed, so a consumer that adds them up must
    #: not find a rate in here.  Three branches built this field independently and
    #: named it `unscored`, `uncredited` and `uncounted`; the latter two survive as
    #: read-only views below, because all three spellings are read by shipped
    #: consumers -- `harbor.py` and `docs/SCHEMA.md` describe the third.
    unscored: dict[str, Any] = field(default_factory=dict)
    #: The ``harness`` block off each stage result that carried one, in the order
    #: the stages were read.  A list and not one value because the three stages
    #: run in three separate images that each take the harness from a mutable tag:
    #: a run whose gate came from one build and whose modules came from another is
    #: a run whose disagreements cannot be attributed, and that has to be visible
    #: rather than resolved by picking one.  Empty for a result written before this
    #: field existed, which is why the report renders nothing instead of "unknown".
    harnesses: list[dict[str, Any]] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        """False when the number reflects the harness rather than the submission."""
        return not self.harness_error

    @property
    def uncredited(self) -> dict[str, Any]:
        """The same dict as `unscored`, under the other branch's name for it.

        An alias rather than a second field: two dicts would be two things to keep
        in step, and the failure mode of letting them drift is a report that says
        NOT RUN in one place and prints the measurement in another.  Both names are
        in the written JSON for the same reason -- a consumer reading either one
        gets the same numbers.
        """
        return self.unscored

    @property
    def uncounted(self) -> dict[str, dict[str, Any]]:
        """`unscored`, in the third branch's shape as well as under its name.

        Not an alias like `uncredited`, because this spelling disagrees about more
        than the name: it reports `modules` as a count where `unscored` carries the
        rows, and it states `ran`/`measured` as flags where `unscored` leaves them
        implied by which keys are present.  `harbor.py` publishes
        `stage2_uncounted_modules` through `int()`, so handing it the list of module
        dicts is a TypeError, and `docs/SCHEMA.md` documents the count.

        Derived rather than stored, so the two shapes cannot drift into disagreeing
        about whether a stage ran -- which is the failure this whole field exists to
        prevent, and it would be a poor joke to reintroduce it one level up.  A
        projection also means nothing writes here: `to_dict` publishes it, and
        `from_dict` restores `unscored`, from which this is recomputed.

        `measured` is false when the measurement could not be taken.  Two ways for
        that, and only the first was covered: `unscored` records an unreadable
        result as an `error` key, and a stage that ran but got no module as far as
        starting records nothing of the kind -- `_record_unscored` grades it
        through a scratch verdict, which succeeds, and the entry arrives with no
        `error`, a `rate` of 0.0 and an empty module table.  So `measured` was true
        for a stage that measured nothing, and the arithmetic line downstream
        offered to exclude measurements that were never taken.  A stage that ran
        and scored a legitimate 0.00 over real modules is still measured: the
        distinguishing fact is whether any module reported, not what it reported.

        The module count is consulted through the same three spellings the loop
        below normalises, because this flag has to mean the same thing on a verdict
        re-read from a file that stored only the count as it does on a fresh one.
        Stage 3 is exempt: it has no modules, and its own absence is already an
        `error`.
        """
        out: dict[str, dict[str, Any]] = {}
        for stage, got in self.unscored.items():
            if not isinstance(got, dict):
                continue
            measured = "error" not in got
            if measured and stage == "behavioural":
                mods = got.get("modules")
                count = (_reported(mods) if isinstance(mods, list)
                         else mods if mods is not None
                         else got.get("module_count") or 0)
                measured = bool(count)
            view: dict[str, Any] = {"ran": True, "measured": measured}
            # `complete` is carried through because stage 2 is all-or-nothing:
            # `points` and `rate` do not determine each other, so a projection
            # keeping only those two shows "0.00 points" beside "rate 0.9800" and
            # leaves a reader of this shape -- the shape `harbor.py` and
            # `docs/SCHEMA.md` describe -- with an apparent arithmetic error and no
            # way to resolve it.  It is also the field the flag is asserted on, since
            # `points` here is what was paid and the rate beside it is not what the
            # payment was decided by.
            for key in ("points", "rate", "complete", "models_total",
                        "models_survived", "status", "error"):
                if key in got:
                    view[key] = got[key]
            mods = got.get("modules")
            if isinstance(mods, list):
                view["modules"] = len(mods)
            elif mods is not None:
                view["modules"] = mods
            elif "module_count" in got:
                # Re-read from a file that only ever stored the count; see
                # `_unscored_from_raw`.
                view["modules"] = got["module_count"]
            # One sentence for the reader, from whichever field the stage put it in.
            # `notes` is the stage's own account of its own number -- which module
            # died, which one never started; a harness error outranks it, because a
            # note about a measurement that could not be taken describes something
            # that did not happen.  Only the LAST note is projected, so on a stage
            # that appended several this sentence is the final one rather than the
            # fullest.  The rest are not lost -- they are in
            # `unscored[stage]["notes"]`, which keeps the list -- and a reader who
            # needs to know which module died reads there.
            note = got.get("note") or got.get("harness_error") or ""
            if not note:
                notes = got.get("notes") or []
                note = str(notes[-1]) if notes else ""
            if note:
                view["note"] = note
            # Renamed on purpose.  `blocked_by` at the top level of a verdict means
            # the rung that stopped the ladder; inside here it means this stage's own
            # verdict on itself, and the two are routinely different -- a gate failure
            # above a stage 2 with an incomplete module sets both, differently.
            if got.get("blocked_by"):
                view["stage_blocked_by"] = got["blocked_by"]
            out[stage] = view
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "task": self.task,
            # Harbor reads `reward` as the normalised headline number; `score` is
            # the same result on the published 100-point scale.  Both are written
            # so neither consumer has to rescale and get it wrong.
            "reward": round(self.score / self.max_score, 6) if self.max_score else 0.0,
            "score": round(self.score, 4),
            "max_score": self.max_score,
            "valid": self.valid,
            "audit_gate": self.audit_gate,
            "behavioural": {
                "points": round(self.behavioural_points, 4),
                "rate": round(self.behavioural_rate, 6),
                "modules": [m.to_dict() for m in self.modules],
            },
            "verification": {
                "points": round(self.verification_points, 4),
                "models_total": self.adversaries_total,
                "models_survived": self.adversaries_survived,
                "breaks": self.verification_breaks,
            },
            "stages_run": self.stages_run,
            "unscored_stages": self.unscored_stages,
            "blocked_by": self.blocked_by,
            # Which harness graded each stage.  Written even when empty, so a
            # reader can tell "no stage recorded one" from "this key predates
            # provenance"; `harness_error` below is a different axis entirely --
            # that one is about the run failing, this one about who ran it.
            "harnesses": self.harnesses,
            "harness_error": self.harness_error,
            "audit_failures": self.audit_failures,
            "notes": self.notes,
            "uncredited": self.uncredited,
            "metadata": self.metadata,
            # All three deliberately outside "behavioural"/"verification": a consumer
            # that adds up the published numbers must not be able to reach these by
            # walking the same keys it sums.
            "unscored": self.unscored,
            "uncounted": self.uncounted,
        }

    def write(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, indent=2)
                fh.write("\n")
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Verdict":
        """Reconstruct a verdict, so a report can be re-rendered from the JSON.

        The stored form nests behavioural and verification; the dataclass keeps them
        flat, because the scorer builds it field by field.  This is the one place
        that knows both shapes.
        """
        fn = raw.get("behavioural") or {}
        adv = raw.get("verification") or {}
        return cls(
            task=str(raw.get("task", "")),
            score=float(raw.get("score", 0.0)),
            max_score=float(raw.get("max_score", 100.0)),
            audit_gate=str(raw.get("audit_gate", "not-run")),
            behavioural_points=float(fn.get("points", 0.0)),
            behavioural_rate=float(fn.get("rate", 0.0)),
            verification_points=float(adv.get("points", 0.0)),
            adversaries_total=int(adv.get("models_total", 0)),
            adversaries_survived=int(adv.get("models_survived", 0)),
            stages_run=list(raw.get("stages_run") or []),
            unscored_stages=dict(raw.get("unscored_stages") or {}),
            blocked_by=str(raw.get("blocked_by", "")),
            harness_error=str(raw.get("harness_error", "")),
            modules=[ModuleScore(
                id=str(m.get("id", "")),
                title=str(m.get("title", "")),
                weight=float(m.get("weight", 0.0)),
                rate=float(m.get("rate", 0.0)),
                # Derived from the rate when the field is absent, so a report
                # rendered from a verdict that predates the column still shows the
                # column: a module at rate 1.0 passed everything it scored.  The
                # `scored_weight` term is what keeps a reporting module -- rate 0.0
                # because it scored nothing -- from reading as complete.
                complete=bool(m.get("complete",
                                    float(m.get("rate", 0.0)) >= 1.0
                                    and float(m.get("scored_weight", 0.0)) > 0)),
                passed=int((m.get("checks") or {}).get("passed", 0)),
                failed=int((m.get("checks") or {}).get("failed", 0)),
                errored=int((m.get("checks") or {}).get("errored", 0)),
                skipped=int((m.get("checks") or {}).get("skipped", 0)),
                status=str(m.get("status", "")),
                # `required` is deliberately not read back.  A stage is free to put
                # whatever it likes in a module's metadata, and reading that key here
                # would let the stage under test mark its own module as special in
                # the score computed over it.
                note=str(m.get("note", "")),
                observations=[str(o) for o in (m.get("observations") or [])],
            ) for m in fn.get("modules") or []],
            audit_failures=list(raw.get("audit_failures") or []),
            verification_breaks=list(adv.get("breaks") or []),
            notes=list(raw.get("notes") or []),
            metadata=raw.get("metadata") or {},
            # Any of the three spellings, because a verdict written before they were
            # reconciled carries only one.  `to_dict` writes all three, so a round
            # trip is stable whichever key an older file used.  Absent from every
            # score.json written before the field existed at all; an empty map reads
            # as "nothing was discarded", which is what those runs recorded.
            unscored=_unscored_from_raw(raw),
            # Copied verbatim, non-dict entries dropped.  Re-stamping with the
            # harness doing the reading would make every re-rendered report claim
            # this run graded it; an empty list is the honest answer for a verdict
            # written before stages recorded who ran them.
            harnesses=[h for h in (raw.get("harnesses") or []) if isinstance(h, dict)],
        )

    @classmethod
    def read(cls, path: str | Path) -> "Verdict":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


#: A module's failures have to agree this strongly before one of them is allowed to
#: speak for the rest.  Two thirds, and at least three of them: a module where half
#: the checks fail one way and half another has no single thing to say, and saying
#: one anyway would be the report naming a cause it does not have.
_SHARED_CAUSE_FRACTION = 2 / 3
_SHARED_CAUSE_MIN = 3


def _shared_cause(checks: list[Check]) -> str:
    """One line for the report when a module's failures are all the same failure.

    Stage 2's report prints the module table and nothing else -- no check summaries,
    no details -- so a module that fails is eleven digits and a rate.  That is
    adequate when the failures are diverse and actively misleading when they are not.

    Measured on fw02's round-3 submission, which did not boot: the report rendered
    eleven modules at rate 0.0000 with 6552 errors between them and named no cause
    anywhere, while ``behavioural.json`` carried ``launcher exited with 1 before
    becoming ready`` 3672 times and the yargs TypeError behind it just as often.  A
    port that does not start is the likeliest shape a real submission takes, and it
    was the shape the report served worst.

    What this says is deliberately a restatement and not a diagnosis: these N checks
    reported the *identical* headline, which is a fact about the artifact rather than
    an inference about the repo.  A module whose failures disagree gets nothing,
    because the honest note there is the table itself.
    """
    # Weight 0 is excluded because this note explains a *rate*, and a weight-0 check
    # cannot move one.  fw02's State A fails four weight-0 detection checks in
    # `unseen-data` -- X-Powered-By, which State A sends because Express does -- so
    # without this the identity run's own full-marks report carried "4 of 4 failing
    # checks report the same thing" against a module scoring 1.0000.  Correct in every
    # word and misleading as a whole, which is the failure mode a note is for.
    # `judged`, so a skip is not read for a shared cause.  A skip is charged now,
    # but its summary says why the check could not run, not what the artifact got
    # wrong -- and 2418 of them in fw04 carry one identical sentence, which would
    # dominate this note on every cell and explain nothing about the submission.
    bad = [c for c in checks if c.judged and not c.ok and c.weight > 0]
    if len(bad) < _SHARED_CAUSE_MIN:
        return ""
    lines = [" ".join((c.summary or "").split()) for c in bad]

    def dominant(keys: list[str]) -> tuple[str, int]:
        tally: dict[str, int] = {}
        for k in keys:
            if k:
                tally[k] = tally.get(k, 0) + 1
        if not tally:
            return "", 0
        key, hits = max(tally.items(), key=lambda kv: kv[1])
        if hits < _SHARED_CAUSE_MIN or hits < len(bad) * _SHARED_CAUSE_FRACTION:
            return "", 0
        return key, hits

    whole, hits = dominant(lines)
    if whole:
        return f"{hits} of {len(bad)} failing checks report the same thing: {whole}"[:400]

    # The headlines disagree, which does not mean the failures do.  ``_headline``
    # builds a summary as "<what was being done>: <what went wrong>", so a module
    # that drives one session per check gets a different subject every time and an
    # identical cause every time: fw02's `write` module failed 1840 checks across
    # seven sessions, i.e. seven distinct headlines, none of them near a two-thirds
    # share -- and every one of them ending "launcher exited with 1 before becoming
    # ready".  Grouping on the clause after the last colon recovers exactly that,
    # and it is coupled to ``_headline`` on purpose: the clause exists because that
    # function put it there.
    #
    # Said differently from the case above, because it is a weaker claim.  These
    # checks did not report the same thing; they ended on the same cause.
    clause, hits = dominant([ln.rsplit(": ", 1)[-1] if ": " in ln else ""
                             for ln in lines])
    if clause:
        return (f"{hits} of {len(bad)} failing checks end on the same cause: "
                f"{clause}")[:400]
    return ""


# --------------------------------------------------------------------------- #
# Stage 1
# --------------------------------------------------------------------------- #


def grade_audit(result: StageResult | None, verdict: Verdict) -> bool:
    """Decide the gate.  Returns True when the behavioural stage may run.

    The gate is the conjunction of the *required* checks.  A non-required
    audit check is an observation the report carries but does not gate on —
    the audit prompt says which is which, so a reviewer reading the prompt can
    see the whole gate without reading any code.

    An ``error`` on a required check is not a fail.  It means the audit could not
    reach a verdict — the model timed out, the API broke, the evidence could not
    be grounded.  Calling that a cheat would punish a submission for the
    grader's outage, so it stops the ladder as a harness error instead.

    But it only stops the ladder when it could still change the answer.  The gate
    is a conjunction, so one required check that failed on grounded evidence
    settles it: no re-run of an undecided *other* check can make the conjunction
    true, and reporting "the grader could not decide" over the top of a decided
    failure hides the finding and asks for a re-run that has nothing to compute.
    Failures are therefore read first, and an undecided check alongside them is
    carried as a note on a result whose verdict is ``fail``.  Undecided blocks
    only when nothing else decided.
    """
    if result is None:
        verdict.audit_gate = "not-run"
        verdict.harness_error = "the audit stage produced no result file"
        verdict.blocked_by = "audit-missing"
        return False

    verdict.stages_run.append("audit")
    verdict.metadata["audit"] = dict(result.metadata)
    if result.harness:
        verdict.harnesses.append(dict(result.harness, stage="audit"))

    if result.status != "ok":
        verdict.audit_gate = "not-run"
        verdict.harness_error = (
            result.metadata.get("error")
            or "; ".join(result.notes)
            or "the audit stage reported status=error"
        )
        verdict.blocked_by = "audit-error"
        return False

    _carry_notes("stage 1", result, verdict)

    # A quorum that shrank is recorded even though it changes no outcome.  When
    # some reviews die and one survives, audit.py notes it and keeps
    # status=ok -- and a stage's own notes are read only on the error branch
    # above, so without this the report cannot tell a gate decided by three
    # reviews from one decided by the only review that came back.  The two read
    # identically today.  Taken from metadata rather than by matching the note's
    # wording, which is prose and may be reworded.
    requested = result.metadata.get("samples_requested")
    usable = result.metadata.get("samples_usable")
    if (isinstance(requested, int) and isinstance(usable, int)
            and 0 < usable < requested):
        verdict.notes.append(
            f"stage 1 ran on a reduced quorum: {usable} of {requested} "
            f"reviews completed, and carried every gate verdict below"
        )

    required = [c for c in result.checks if c.required]
    if not required:
        # A gate with nothing mandatory would pass every submission including the
        # untouched State A, so an empty gate is an authoring bug, not a pass.
        verdict.audit_gate = "not-run"
        verdict.harness_error = (
            "the audit result declares no required checks; a gate that "
            "cannot fail is not a gate"
        )
        verdict.blocked_by = "audit-empty"
        return False

    # A definite failure is decided before an undecided one is consulted.  Both
    # orders zero the score, but they say different things: "the gate failed" is
    # a result about the submission, while "could not be decided" is a result
    # about the run and asks for a re-run.  A re-run cannot lift a gate that
    # another required check already failed on grounded evidence, so reporting
    # the second when the first is available would invalidate a verdict that was
    # never in doubt -- the same reasoning verification.py applies to a round it
    # declines to call undecided.
    failures = [c for c in required if c.verdict == "fail"]
    errored = [c for c in required if c.verdict == "error"]
    if failures:
        # Decided, whatever the undecided ones would have said.  They are named
        # in the notes rather than in audit_failures, which harbor.py counts
        # as stage1_failures -- an undecided check is not a failure and must not
        # inflate that number.
        verdict.audit_failures = [_failure(c) for c in failures[:40]]
        verdict.audit_gate = "fail"
        verdict.blocked_by = "audit-gate"
        # "and no further stage was run" was here and was an assumption about the
        # operator, not something this function can see: it has only the audit
        # result, the stages are separately runnable, and the caller loads every
        # result file that exists before grading.  So the note says what the gate
        # actually decides -- that nothing a later stage measured can be credited
        # -- and it must not contradict the table printed directly above it.
        verdict.notes.append(
            f"audit gate failed on {len(failures)} required check(s); "
            f"score is 0 and no later stage can earn anything"
        )
        if errored:
            # Recorded, not promoted: the reader should know some gates went
            # unanswered, without that turning a decided failure into an invalid
            # run.  A re-run cannot lift a gate another required check already
            # failed on grounded evidence.
            verdict.metadata["audit_undecided"] = [c.id for c in errored]
            verdict.notes.append(
                f"{len(errored)} further required check(s) could not be decided "
                + "(" + ", ".join(c.id for c in errored[:6]) + "); "
                "the gate is already decided by the failure(s) above, so this "
                "did not affect the outcome and does not call for a re-run"
            )
        return False

    if errored:
        verdict.audit_gate = "not-run"
        verdict.harness_error = (
            f"{len(errored)} required audit check(s) could not be decided: "
            + ", ".join(c.id for c in errored[:6])
        )
        verdict.blocked_by = "audit-undecided"
        verdict.audit_failures = [_failure(c) for c in errored[:40]]
        return False

    # Cleared on the passing path: a gate that passed has no failures to show,
    # and leaving a list behind would put them in harbor's stage1_failures count.
    #
    # The incoming branch re-detected `failures` here and raised the gate on them.
    # Not kept, because by this line both `if failures:` and `if errored:` above
    # have returned, so that test can never be true -- and it arrived in that shape
    # because on its own branch the errored check came FIRST.  That ordering is the
    # part worth naming: with `errored` first, a submission that genuinely failed a
    # gate and also had one undecided check is reported "could not be decided,
    # re-run" instead of "failed", so a decided failure leaves as a harness
    # problem.  A decided failure outranks an undecided check, which is why the
    # order above is the one kept.
    verdict.audit_failures = []
    verdict.audit_gate = "pass"
    return True


def _failure(check: Check) -> dict[str, Any]:
    return {
        "id": check.id,
        "verdict": check.verdict,
        "summary": check.summary,
        "detail": check.detail[:2000],
        "evidence": check.evidence[:12],
    }


def _carry_notes(stage: str, result: StageResult, verdict: Verdict) -> None:
    """Carry a stage's own notes into the verdict on the ``ok`` path.

    ``result.notes`` was read in three places and all three were error branches,
    so a stage that ran fine and recorded something rendered *identically* to one
    that had nothing to say.  That is the failure mode where a degraded run --
    a retried sample, a module that fell back, a suite that skipped a fixture --
    is indistinguishable from a clean one in the delivered report.  The only
    surviving trace was ``samples_usable`` buried in metadata, which nothing
    prints.  It is the wrong way round: an error announces itself in the headline
    anyway, and it is precisely the run that *passed* while degraded whose
    degradation a reader would otherwise never learn about.

    The stage name is prefixed because the notes section is shared: a reader
    seeing "one sample was retried" needs to know which stage said it.

    Called on the ``ok`` path only, and once per stage.  Two branches wrote this
    function, one taking the label and one deriving it from ``result.stage``, and
    both definitions merged with no conflict -- the second shadowed the first, so
    every two-argument call raised TypeError and 45 tests failed at once.  The
    label is passed rather than derived because ``result.stage`` spells it
    ``audit``/``behavioural`` while the report's own stage lines say
    ``stage 1``/``stage 2``, and these notes are read next to those lines.

    On the error path the note is already the ``harness_error``, so carrying it
    here as well printed the same sentence twice; the other branch called this
    before the status check and that is what its test caught.  The dedupe guard is
    the other branch's, kept: it costs nothing and a stage that records one
    sentence twice should not read as two degradations.
    """
    for note in result.notes:
        text = str(note).strip()
        line = f"{stage}: {text}"
        if text and line not in verdict.notes:
            verdict.notes.append(line)


# --------------------------------------------------------------------------- #
# Stage 2
# --------------------------------------------------------------------------- #


def grade_behavioural(
    result: StageResult | None, verdict: Verdict, policy: ScoringPolicy
) -> bool:
    """Score the modules.  Returns True when the verification stage may run.

    The stage is all-or-nothing: it pays ``policy.behavioural_points`` when every
    scored check in every weighted module passed, and nothing otherwise.  So the
    question this answers is "is the port finished", and one failing check is the
    same answer as a tree that never compiled.

    That is a claim about what stage 2 can and cannot measure.  A rate is a fair
    summary of a suite only if its checks are interchangeable, and these are not:
    a task's corpus is thousands of near-identical cases while its packaging module
    is four, so a rate is mostly a report on the largest suite and a single missing
    behaviour disappears into it.  Weights were the previous answer to that and
    they only move the problem -- they make the arithmetic defensible without
    making the number mean "works".

    The rate is still measured, and still published on ``verdict.behavioural_rate``
    and every module row, because it is the evidence that separates a port failing
    one edge case from one that never built.  It just does not buy anything.

    Weight is what decides whether a module can block: a module carrying weight
    has to be complete, and a weight-0 module reports without judging and cannot.
    Everything else falls out of that -- a module that crashed, timed out or was
    never reached has no complete row and stops the stage, with no separate rule
    needed to say so.
    """
    if result is None:
        verdict.harness_error = "the behavioural stage produced no result file"
        verdict.blocked_by = "behavioural-missing"
        return False

    verdict.stages_run.append("behavioural")
    verdict.metadata["behavioural"] = dict(result.metadata)
    if result.harness:
        verdict.harnesses.append(dict(result.harness, stage="behavioural"))

    if result.status != "ok":
        verdict.harness_error = (
            result.metadata.get("error")
            or "; ".join(result.notes)
            or "the behavioural stage reported status=error"
        )
        verdict.blocked_by = "behavioural-error"
        return False

    _carry_notes("stage 2", result, verdict)

    if not result.units:
        verdict.harness_error = "the behavioural stage declared no modules"
        verdict.blocked_by = "behavioural-empty"
        return False

    scores: list[ModuleScore] = []
    for unit in result.units:
        checks = result.checks_of(unit.id)
        counts = {v: sum(1 for c in checks if c.verdict == v)
                  for v in ("pass", "fail", "error", "skip")}
        passed, total = pooled(checks)
        rate = (passed / total) if total > 0 else 0.0
        note = ""

        if unit.status != "ok":
            # The module did not finish, so the rate is 0.0 -- what it managed to
            # record is a floor under the submission's behaviour and not a measurement
            # of it, and paying that floor would let a module be made cheap by being
            # made slow.  A module that timed out, errored, or was never reached is an
            # ordinary zero and nothing more: it is charged, and it does not make the
            # run invalid.  A slow module and a wrong one cost exactly the same, which
            # is the point -- the alternative is a suite where the fastest way to avoid
            # a bad score is to not finish.
            rate = 0.0
            note = unit.summary or f"module status={unit.status}"
        elif total <= 0:
            # An empty denominator scores zero and keeps its weight, so a module
            # cannot be made cheap by making it unanswerable.
            #
            # Two independent questions decide what to say about that zero, and
            # two branches each answered one of them.  Both are asked here.
            #
            # Was the module DECLARED at weight zero?  Then it is a reporting
            # module -- it records and does not judge -- and "produced no scored
            # checks" is what success looks like for it, not a failure.  A task
            # whose module is meant to stay inert carries weight 0.00 exactly so:
            # lang04's provenance module does this on a JavaScript tree.
            #
            # And did it decline its checks or record none at all?  Every check
            # skipped as inapplicable is a design statement about this tree; no
            # checks at all is a module that measured nothing.  Whoever has to act
            # on the zero needs to know which, and it is not recoverable from the
            # rate.
            rate = 0.0
            how = ("every check declined as inapplicable" if counts["skip"]
                   else "it recorded no checks at all")
            if unit.weight <= 0.0:
                note = (f"reports only, by design -- weight zero, and {how}, so "
                        f"it decides nothing")
            else:
                note = (f"module produced no scored checks despite carrying "
                        f"weight {unit.weight:g} ({how}); its 0.0 rate is charged "
                        f"to the submission and may not belong to it")
        # No branch here clamps the rate to zero on a failed check, and no check can
        # ask for one.  A module's checks each carry one vote: "the build succeeded"
        # failing costs its own vote and nothing else.  A clamp would discard the
        # module's own measurement of how much else was true while charging nothing
        # extra -- the checks downstream of a failed build report `error` and are
        # already counted as failures.  Whether a build failure is worth more than one
        # check is a question for the weights, not for a clamp inside a module.  What
        # stops the stage is the ladder, above: a weighted module that did not pass
        # every scored check pays nothing for the stage, whichever check it was.

        # Skips are charged, so a declined check is a scored zero and the reader is
        # owed the fact that it did not run.  Not said from the `total <= 0` branch
        # above, which an all-skip module does not reach: six skips pool as 0/6 rather
        # than 0/0, so it takes the ordinary path -- the one shape where the rate is
        # least self-explanatory is the one that needs the note most.
        #
        # Stated as a count over the denominator rather than as "all of them",
        # because the partial case is the one that misleads: 40/50 reads as a module
        # the submission mostly passed, and if 10 of the 50 never ran then what it
        # passed was 40 of 40.  Charging them is right -- another submission answered
        # those 10 -- but the reader has to be able to see the difference
        # between a wrong answer and an absent one.
        if not note and counts["skip"]:
            note = (f"{counts['skip']} of {total:g} scored check(s) did not run and "
                    f"are charged 0"
                    + (" -- every check in this module declined, so its 0.0 is the "
                       "absence of an artifact to measure rather than a measurement"
                       if counts["skip"] >= total > 0 else ""))

        # The scorer's own note, when it has nothing better to say than the shape
        # of the failures: a module whose failures are all one failure says so,
        # rather than leaving the reader eleven modules at 0.0000 and no cause.
        if not note:
            note = _shared_cause(checks)

        # behavioural.py folds a module's own notes into `unit.metadata["notes"]`
        # on the way in.  They stopped there: the report printed the scorer's
        # note and nothing the module said, so `structure` appeared rated over
        # the cases it asked with no sign of the ones it declined -- which is
        # the confusion driver.py's note was written to prevent, in its words
        # "in the report as well as the log".
        #
        # Kept separate from `note` above, and not folded into it: that one is
        # the scorer's sentence about the rate and this is the module's about its
        # own run.  Concatenating them would make a module's explanation
        # disappear whenever the scorer happened to have a note of its own.
        own_notes = (unit.metadata or {}).get("notes") or []
        if isinstance(own_notes, str):
            own_notes = [own_notes]

        # A reporting module's findings, so they reach the readable report and not
        # only the JSON.  A different field from `own_notes` above and gathered
        # from a different place: those come from the module's result metadata and
        # are about its own run, these are the weight-zero checks themselves and
        # are about the submission.  Restricted to modules that score nothing:
        # those are the ones whose whole output is observations, and the ones whose
        # row would otherwise carry a rate of 0.0000 and no way to tell why that is
        # fine.  Capped, because a corpus-sized module could otherwise bury the
        # table.
        observations: list[str] = []
        if total <= 0:
            # Weight, not `judged`.  `Check.judged` is "not a skip", so a weight-0
            # note passes it -- filtering on `judged` here would silently select
            # nothing at all, which is the failure this whole change is about.
            recorded = [c for c in checks if c.weight <= 0 and c.summary]
            for c in recorded[:_MAX_OBSERVATIONS]:
                observations.append(_clip_observation(c.summary))
            if len(recorded) > _MAX_OBSERVATIONS:
                observations.append(
                    f"({len(recorded) - _MAX_OBSERVATIONS} further observation(s) "
                    f"in results/behavioural.json)")

        scores.append(ModuleScore(
            id=unit.id, title=unit.title or unit.id, weight=unit.weight, rate=rate,
            # `total > 0` and not `rate >= 1.0` alone: `pooled` returns a zero
            # denominator for a module whose every check is an observation, and
            # `rate` hands that 0.0 rather than 1.0 -- but a module that scored
            # nothing has not passed anything either, and calling it complete would
            # let an empty weighted module clear the stage.
            complete=bool(total > 0 and passed >= total),
            passed=counts["pass"], failed=counts["fail"], errored=counts["error"],
            skipped=counts["skip"], status=unit.status, note=note,
            notes=[str(n) for n in own_notes if str(n).strip()],
            scored_weight=total, observations=observations,
        ))

    verdict.modules = scores
    wsum = sum(m.weight for m in scores)
    if wsum <= 0:
        verdict.harness_error = "behavioural module weights sum to zero"
        verdict.blocked_by = "behavioural-weights"
        return False

    # A charged zero has to be legible wherever it is reached from, so the notes for
    # the two ways a module can score zero without being measured -- it overran its
    # budget, or the stage clock ended before it started -- are appended before any
    # branch that returns.
    #
    # Both figures, because a reader deciding whether to act on this needs to know
    # whether the module was near its budget or nowhere near it: 2410s against 2400s
    # is a submission at the edge, 3005s against 2400s is one well past it, and only
    # the second would still fail a re-run under a raised budget.  The count it did
    # record is named for the same reason -- it is the evidence that the zero is the
    # deadline's and not an empty module's.
    overran = [m for m in scores if m.status == "timeout"]
    if overran:
        verdict.notes.append(
            "behavioural module(s) exceeded the budget the task declares for them, so "
            "each scores zero and keeps its weight: "
            + ", ".join(
                f"{m.id} (weight {m.weight:g}"
                + (f", {m.passed}/{m.passed + m.failed} recorded before the deadline"
                   if (m.passed + m.failed) else ", recorded nothing")
                + ")"
                for m in overran[:6])
            + (f", and {len(overran) - 6} more" if len(overran) > 6 else "")
            + " -- a budget overrun is charged to the submission, because the "
              "reference tree meets the same budget on the same machine"
        )

    # Beside `overran` and for the same reason -- a charged zero has to be legible
    # wherever it is reached from -- but worded so it cannot be read as a
    # measurement.  An overrun module was measured against a budget; an unreached one
    # ran not at all, and the only honest thing to say about it is that the stage
    # clock ended first and its share was charged anyway.  The distinction matters to
    # whoever acts on this: an overrun points at the module named, an unreached list
    # points at the modules *before* it, which are where the clock went.
    absent = [m for m in scores if m.status == "unreached"]
    # Charged even when it is every module.  This used to refer the whole stage back
    # as a harness error on the reasoning that something must have measured the
    # submission before its share can be charged to it -- and an all-unreached stage
    # does not distinguish a submission that ate the clock from an image that hung on
    # startup.  That reasoning is now overruled deliberately: a stage that did not
    # finish is a zero, not an invalid result.  It has to be, because the alternative
    # makes "run long enough that nothing completes" the cheapest way out of a bad
    # score, and it leaves a real corpus with rows that carry no number at all.  A
    # genuinely broken image is still visible -- as `status` on every module and as
    # this note -- and is re-run because a reader decided to, not because the grader
    # withheld the number.
    if absent:
        verdict.notes.append(
            "the behavioural stage ended before "
            f"{len(absent)} of {len(scores)} module(s) were reached, so each scores "
            "zero and keeps its weight: "
            + ", ".join(f"{m.id} (weight {m.weight:g})" for m in absent[:6])
            + (f", and {len(absent) - 6} more" if len(absent) > 6 else "")
            + " -- the stage clock is the task's own declaration and the reference "
              "tree completes the same suite well inside it, so the exhaustion is "
              "charged to the submission; the modules that did run are where it went"
        )

    # No module zeroes the stage by itself, because no module has to: every weighted
    # module must pass every scored check for the stage to be paid at all, so a
    # single failing check anywhere costs the same as a module-wide veto would.  What
    # a weight still decides is the rate the report publishes -- how much of a
    # submission worked, which is worth knowing about a tree that was not paid -- and
    # whether a module can judge at all, since a weight-0 module reports without
    # blocking.

    # A module that did not finish at all -- a signal, an OOM kill, a container that
    # died, a module that exited zero having written nothing.  It scores zero, keeps
    # its weight, and is reported; it does not invalidate the stage.
    #
    # The alternative is to refuse a number: the harness cannot attribute a dead module
    # to the tree under test, so arguably it must not charge what it cannot attribute.
    # That is sound about causes and wrong about consequences -- it would make every
    # module a veto over the whole result, so one crash in one module of sixteen would
    # withhold the fifteen real measurements beside it.  The unattributability is real
    # and is said in a note rather than enforced by withholding: `status` is on every
    # module row, so a reader who wants to re-run can see which module to re-run and
    # decide, while the modules that did measure the submission still publish what they
    # measured.
    #
    # Timeout and error need no distinction here for the same reason -- both are zeros
    # that keep their weight -- so `_is_charged` shapes only the wording of the notes
    # above, never the number.
    dead = [m for m in scores if not _is_charged(m.status)]
    if dead:
        weighted = [m for m in dead if m.weight > 0]
        verdict.notes.append(
            "behavioural module(s) did not finish, so each scores zero and keeps its "
            "weight: "
            + ", ".join(
                f"{m.id} (status {m.status}"
                + (f", weight {m.weight:g}" if m.weight > 0 else "") + ")"
                for m in dead[:6])
            + (f", and {len(dead) - 6} more" if len(dead) > 6 else "")
            + (" -- a module that did not finish has not measured the submission, so "
               "if this reproduces, the module or its image may be at fault rather "
               "than the tree under test" if weighted else
               " -- these carry no weight, so the score is unaffected; their "
               "observations are simply absent from this report")
        )

    # The measurement, published for a reader and multiplied by nothing.  Written
    # before the branch below so that a stage that pays zero still says how much of
    # the suite passed: that is the difference between a port failing one edge case
    # and a port that never built, and the points cannot carry it.
    verdict.behavioural_rate = sum(m.weight * m.rate for m in scores) / wsum

    # The stage's verdict, read off the rows rather than off the rate: `complete` is
    # what each row contributes, and deriving the same fact from `behavioural_rate`
    # here would be a second expression of the arithmetic the table already shows,
    # which is how a printed figure and a scored figure drift apart.  Weighted rows
    # only -- a weight-0 module reports without judging, so it cannot block.
    short = [m for m in scores if m.weight > 0 and not m.complete]
    if short:
        verdict.behavioural_points = 0.0
        verdict.blocked_by = "behavioural-incomplete"
        # Says "the verification stage" and not "stage 3" deliberately.  The report
        # renders notes as wrapped body text under the stage lines, and six tests
        # locate a stage line by `"stage 3" in line` -- a note carrying that
        # substring is indistinguishable from a second stage line to every one of
        # them.  The stage's other name is used in the clause before this, so
        # nothing is lost.
        #
        # "no points are available", not "the stage was not run": whether a driver
        # ran it is a fact about the run, and this function has not looked.  When one
        # did, `_record_unscored` says so a few lines later, and the two sentences
        # must not contradict each other on the same report.
        named = ", ".join(
            f"{m.id} ({m.passed}/{m.passed + m.failed + m.errored + m.skipped}"
            + (f", status {m.status}" if m.status != "ok" else "") + ")"
            for m in sorted(short, key=lambda x: -x.weight)[:6])
        verdict.notes.append(
            f"{len(short)} behavioural module(s) did not pass every scored check, so "
            f"stage 2 pays none of its {policy.behavioural_points:g} points and no "
            f"verification points are available: {named}"
            + (f", and {len(short) - 6} more" if len(short) > 6 else "")
            + f" -- the modules passed {verdict.behavioural_rate:.4f} of their "
              f"weighted checks, which is reported and not paid"
        )
        return False

    verdict.behavioural_points = policy.behavioural_points
    return True


def behavioural_reaches_gate(
    result: StageResult | None, policy: ScoringPolicy, task: str = "",
) -> tuple[bool, float, str]:
    """Would this stage-2 result let the ladder reach stage 3?

    For a caller deciding whether to *run* stage 3, which the ladder above only
    decides whether to *pay* for.  Returns whether every scored check passed, the
    figure stage 2 was paid, and one sentence for an operator.

    It grades against a throwaway verdict rather than recomputing the rate, for
    the reason ``_record_unscored`` gives: a second expression of the same
    arithmetic is how the figure a reader sees and the figure the scorer pays
    drift apart.  So a stage 3 that this function declines to run and a stage 3
    the scorer would have refused to pay for cannot disagree -- there is one
    implementation, and this is a second question asked of it.

    Every way stage 2 can fall short comes along for free, which is the point of not
    special-casing the comparison: whatever stopped the stage -- a module that
    failed a check, one that never finished, a weightless table -- stops it here
    too, so stage 3 is not run for it either.
    """
    if result is None:
        return False, 0.0, "stage 2 has produced no result file"
    scratch = Verdict(task=task or result.task, max_score=policy.max_score)
    reached = grade_behavioural(result, scratch, policy)
    points = scratch.behavioural_points
    if reached:
        return True, points, (f"stage 2 passed every scored check and paid "
                              f"{points:g} of {policy.behavioural_points:g}")
    # Stage 2 falls short eight ways and only one of them is an incomplete module.
    # The sentence is taken from whichever the grader took rather than mapped from
    # `blocked_by` here: a mapping written against today's eight goes quietly
    # generic on the ninth, and it is the *unusual* fall -- a suite whose container
    # died, a module table with no weight -- where an operator most needs to be told
    # which thing happened.  `harness_error` is set on every path above except the
    # incomplete one, because a check that failed is a fact about the submission and
    # not a fault, so that is the branch that needs a sentence here.  It carries the
    # measured rate: the points are 0 for one failing check and for a tree that never
    # compiled, and an operator deciding whether to look needs those told apart.
    why = scratch.harness_error or (
        f"stage 2 did not pass every scored check, so none of its "
        f"{policy.behavioural_points:g} points are paid; the modules passed "
        f"{scratch.behavioural_rate:.4f} of their weighted checks")
    return False, points, f"{why} [{scratch.blocked_by}]"


def audit_passes_gate(
    result: StageResult | None, policy: ScoringPolicy, task: str = "",
) -> tuple[bool, str]:
    """Did stage 1 pass?  The other half of the question above.

    Two rungs stand between a submission and the verification stage, and both have
    to be asked before the rounds are paid for.  ``behavioural_reaches_gate`` asks
    stage 2, and a submission that fails the audit gate scores zero whatever
    stage 2 measured -- so the rounds it ran are bought and discarded exactly as
    they are for a submission a check short.  That is not hypothetical for a
    hand-driven ladder: the three stages are separately
    runnable, and stage 2 is the one that needs no credentials, so running stage 2
    against a tree whose stage 1 failed and then continuing is the ordinary way to
    arrive here.

    Asked of ``grade_audit`` on a throwaway verdict, for the same reason the
    stage-2 question is asked of ``grade_behavioural``: the decision to *run* and
    the decision to *pay* then cannot disagree, because there is one
    implementation of each gate and this is a second question asked of it.

    ``policy`` is unused and taken anyway.  The gate is a property of the required
    checks rather than of any declared figure, so there is nothing here to read
    out of the policy -- but the caller holds one, both of these functions are
    reached from the same three lines, and a signature that differs only by an
    argument invites the argument being dropped from the wrong call.

    Returns the verdict and one sentence for an operator, never the reason
    ``blocked_by`` holds alone: ``audit-missing``, ``audit-error``,
    ``audit-empty`` and ``audit-undecided`` are four different things to
    do next and only one of them is about the submission.
    """
    del policy  # documented above: the gate reads checks, not figures
    scratch = Verdict(task=task or (result.task if result else ""),
                      max_score=0.0)
    if grade_audit(result, scratch):
        return True, "stage 1 passed every required gate"
    # `harness_error` covers the three ways stage 1 fails to *decide* -- absent,
    # errored, no required check at all -- and is empty on the one path that
    # decided against the submission, where the failed ids are the sentence.
    named = [str(f.get("id", "")) for f in scratch.audit_failures[:6]]
    why = scratch.harness_error or (
        f"the audit gate failed on {len(scratch.audit_failures)} "
        f"required check(s): " + ", ".join(n for n in named if n)
    ) or "the audit gate did not pass"
    return False, f"{why} [{scratch.blocked_by}]"


# --------------------------------------------------------------------------- #
# Stage 3
# --------------------------------------------------------------------------- #


def grade_verification(
    result: StageResult | None, verdict: Verdict, policy: ScoringPolicy,
    *, expected: bool = False,
) -> None:
    """Pay for every adversary that failed to break the submission.

    One unit per adversary; one check per adversary carrying the round's verdict.
    ``pass`` means the model did not produce a valid breaking test within its
    budget, and the submission is paid for it.  ``fail`` means it did, and the
    break is reported so a human can read the test that won.

    A round that errored pays nothing and is recorded as an error, not as a
    break: an adversary whose API call failed has not demonstrated a defect.
    That is also why an errored round makes the verdict invalid rather than
    cheap.  Paying 50 of 60 because one gateway returned 503 charges the outage
    to the submission, in the field `flatten` publishes as `stage3_points`
    beside `reward` -- and `docs/SCHEMA.md` names this exact case when it says a
    stage that did not run produces "a valid: false verdict that says re-run,
    never a published zero".

    ``expected`` is whether this ladder was supposed to have a stage-3 result:
    the task declares the stage and the two rungs below it passed.  Callers that
    are grading a subset -- every by-hand invocation of stages 1 and 2, and the
    scorer's own report-only shadow passes -- leave it False, and a missing
    result stays what it is for them, a stage they did not ask about.  The two
    production callers pass the task's declared stages, so for them a stage 3
    that is declared and absent is the harness failing to run it.
    """
    if result is None:
        verdict.metadata["verification_missing"] = True
        if expected:
            # 60 of the 100 points were never contested.  Publishing the other 40
            # as a score says six adversaries tried and none succeeded, which is
            # the same sentence a submission that actually survived them earns:
            # `score`, `valid` and `harness_error` were byte-identical between a
            # tree that beat every adversary and a run whose stage 3 never
            # started.
            verdict.harness_error = (
                f"the verification stage was declared and the ladder reached it, "
                f"but it produced no result file, so "
                f"{policy.verification_points:g} of the "
                f"{policy.max_score:g} points were never contested"
            )
            verdict.blocked_by = "verification-missing"
            return
        verdict.notes.append(
            "the verification stage produced no result file; no stage-3 points "
            "were awarded"
        )
        return

    verdict.stages_run.append("verification")
    verdict.metadata["verification"] = dict(result.metadata)
    if result.harness:
        verdict.harnesses.append(dict(result.harness, stage="verification"))
    # Before the status check here, unlike stages 1 and 2, and the asymmetry is
    # deliberate: their error branches make the notes *be* the harness error, so
    # carrying them again would print one sentence twice.  This branch does not --
    # it writes its own note from `metadata["error"]` and never reads
    # `result.notes` -- so a stage-3 note written by a degraded run that then
    # errored is only on the verdict because of this call.
    _carry_notes("stage 3", result, verdict)

    if result.status != "ok":
        # The stage's own notes are already on the verdict via _carry_notes; this
        # only has to say what the error cost.
        #
        # A harness error and not merely a note, unconditionally -- `expected` does
        # not gate this one.  A result file that exists and reports `status=error`
        # is the stage saying it could not run, which is a fact about the harness
        # however the caller arrived here; the flag above only decides what an
        # ABSENT file means.  `verification.py` sets this status when no round
        # completed at all.
        verdict.harness_error = (
            "the verification stage did not complete"
            + (f": {result.metadata['error']}"
               if result.metadata.get("error")
               else (f": {'; '.join(str(n) for n in result.notes)}"
                     if result.notes else ""))
        )
        verdict.blocked_by = "verification-error"
        verdict.notes.append(
            "no stage-3 points were awarded, and the score is not published: a "
            "stage that could not run has established nothing about the submission"
        )
        return

    rounds = [c for c in result.checks if c.metadata.get("kind") == "round"] or [
        c for c in result.checks if c.unit
    ]
    survived = [c for c in rounds if c.verdict == "pass"]
    broken = [c for c in rounds if c.verdict == "fail"]
    errored = [c for c in rounds if c.verdict in ("error", "skip")]

    verdict.adversaries_total = len(rounds)
    verdict.adversaries_survived = len(survived)
    verdict.verification_breaks = [
        {
            "adversary": c.unit or c.id,
            "summary": c.summary,
            "detail": c.detail[:2000],
            "evidence": c.evidence[:8],
        }
        for c in broken[:24]
    ]

    points = policy.points_per_survived_model * len(survived)
    verdict.verification_points = min(points, policy.verification_points)

    if len(rounds) != policy.verification_models:
        verdict.notes.append(
            f"the probe ran {len(rounds)} adversar{'y' if len(rounds) == 1 else 'ies'} "
            f"but the policy declares {policy.verification_models}; "
            f"stage-3 points are still one per survivor"
        )
    if errored:
        verdict.notes.append(
            f"{len(errored)} verification round(s) could not be completed and paid "
            f"nothing: " + ", ".join((c.unit or c.id) for c in errored[:6])
        )
        # And the verdict says re-run, because "paid nothing" is the problem.  A
        # round is worth `points_per_survived_model`, so an adversary whose gateway
        # returned 503 is priced identically to one that read the tree and found no
        # defect -- the submission is docked 10 points for someone else's outage,
        # and `flatten` publishes the difference as `stage3_points` with `valid: 1`
        # beside it.  `verification.py` only raises the STAGE status when every round
        # errored, so a partial outage arrives here as `status=ok` and this is the
        # only place it can be caught.
        #
        # The points measured are left on the verdict rather than withheld: they
        # are what the rounds that did run established, `valid: false` is what says
        # the total is not final, and a reader deciding whether re-running is worth
        # it needs the figure.  Re-running one adversary is cheap; averaging an
        # outage into a model's score is not recoverable after the fact.
        verdict.harness_error = (
            f"{len(errored)} of {len(rounds)} verification round(s) did not "
            f"complete, so "
            f"{policy.points_per_survived_model * len(errored):g} of the "
            f"{policy.verification_points:g} stage-3 points could not be contested: "
            + ", ".join((c.unit or c.id) for c in errored[:6])
            + (f", and {len(errored) - 6} more" if len(errored) > 6 else "")
            + " -- re-run the stage; an adversary that could not run has not "
              "demonstrated a defect, and must not be paid for as though it had "
              "found one"
        )
        verdict.blocked_by = "verification-incomplete"


# --------------------------------------------------------------------------- #
# The ladder
# --------------------------------------------------------------------------- #



# Three functions on this path, not one, because two branches fixed the same
# defect -- "NOT RUN" printed over a stage whose result file is on disk -- and each
# reaches a case the other does not:
#
#   _report_behavioural_only  copies real ModuleScores onto the verdict, so the
#       report renders the true module table and `flatten` emits correct
#       `module_*_rate` keys.  Behavioural only, and it bails when the result has no
#       modules to tabulate.
#   _record_unscored         records both later stages as dicts under `uncredited`,
#       which is what the report falls back to when the above bailed, and the only
#       account of stage 3.
#   _summarise_unscored      one sentence per stage under `unscored_stages`, which
#       is the only thing said when a stage ran but did not complete.
#
# All three run, none of them is conditional on another having run, and no one of
# them subsumes another: they write disjoint fields that separate consumers read,
# so skipping the "redundant" one silently empties whatever reads only that field.
# None of them appends to `stages_run` -- `grade_behavioural` is the only thing that
# does, because that list is published as `stage2_scored` and an uncredited stage
# was not scored.  `grade_behavioural` is called by two of them against separate
# throwaway verdicts, which is safe -- it reads its result and assigns only onto
# the verdict it was handed -- and by both inside `try`, because a stage whose
# result cannot be parsed must not break the verdict it is only annotating.


def _record_unscored(
    verdict: Verdict,
    policy: ScoringPolicy,
    behavioural: StageResult | None,
    verification: StageResult | None,
) -> None:
    """Note what a later stage measured, when the ladder stopped below it.

    The ladder stopping is a scoring decision and this does not revisit it: the
    score stays whatever policy says, and every field the score is computed from
    is left alone.  What it fixes is a report that says a stage was NOT RUN while
    that stage's own result file sits in the same directory -- which is what the
    driver produces whenever the stages are run by hand rather than short-circuited
    by Harbor, and it is the normal case for anyone driving one task's ladder.
    Saying NOT RUN there is not the conservative reading: it is a false statement
    about a measurement the scorer is holding, made to the person who took it.

    It is also the most useful number in the report when a gate fails.  "Stage 1
    failed" alone does not distinguish a port that works but kept a forbidden
    module-level binding from a port that never compiled anything; the stage-2
    rate does, immediately.  Measured on pf02 run 3: gate fail on
    `capabilities_are_injected`, and stage 2 passing 42.07% of its scored checks
    -- so both were true at once, and only one of them was visible.

    The measurement goes through a scratch verdict so it is the same arithmetic
    the scored path uses.  Building the number a second way here would let the
    reported figure and the scored figure drift, which is worse than not
    reporting it.
    """
    for name, result in (("behavioural", behavioural), ("verification", verification)):
        if result is None:
            continue
        scratch = Verdict(task=verdict.task, max_score=policy.max_score)
        try:
            if name == "behavioural":
                grade_behavioural(result, scratch, policy)
                # `rate` carries this dict's whole reason for existing.  The points
                # are all-or-nothing, so on the discarded path they are 0.0 for a
                # tree passing 80% of every module and 0.0 for one that never
                # compiled; the rate is what tells those apart, and without it a
                # failed gate would discard the only evidence of what the submission
                # does.  `modules` below carries the rows it came from.
                entry: dict[str, Any] = {
                    "points": round(scratch.behavioural_points, 4),
                    "would_be_points": round(scratch.behavioural_points, 4),
                    "rate": round(scratch.behavioural_rate, 6),
                    "complete": bool(scratch.behavioural_points > 0),
                    # Stage 2's own verdict on itself, which the audit gate
                    # does not reach.  An incomplete module stops this stage
                    # independently of stage 1 -- so "fix stage 1 and this becomes
                    # 40 points" can be false, and the reader has to be told which
                    # of the two it is.
                    "blocked_by": scratch.blocked_by,
                    "harness_error": scratch.harness_error,
                    # The reasons `grade_behavioural` wrote, e.g. which modules
                    # overran or went unreached.  On the credited path these reach
                    # `verdict.notes` and get printed; scoring into a scratch
                    # verdict routed them to a value that was dropped.
                    "notes": list(scratch.notes),
                    "modules": [m.to_dict() for m in scratch.modules],
                }
            else:
                grade_verification(result, scratch, policy)
                entry = {
                    "points": round(scratch.verification_points, 4),
                    "models_total": scratch.adversaries_total,
                    "models_survived": scratch.adversaries_survived,
                }
        except Exception as exc:  # pragma: no cover - defensive
            # A stage this one is not scoring must never be able to break the
            # verdict it is only annotating.  This runs after the score is
            # already decided, so a reading that cannot be taken is simply not
            # reported -- it must not turn a decided zero into a harness error.
            entry = {
                "error": f"{type(exc).__name__}: {exc}",
                "note": f"the stage result could not be read: "
                        f"{type(exc).__name__}: {exc}",
            }
        entry["status"] = result.status
        entry["counted"] = False
        verdict.unscored[name] = entry
        # Stage 2 measures a rate and is paid an all-or-nothing figure, so the two
        # have to be named separately: `points` here is what the stage would have
        # been PAID, and calling it what the stage "measured" puts a measurement
        # word on an award.  A sentence carrying only the award cannot reconcile
        # itself either -- "measured 0.00 of its points" is what a tree one check
        # short earns and what a tree that failed every check earns.  So the rate
        # is reported as the measurement and the points as the award, and stage 3,
        # which measures no rate, keeps the single-figure wording.
        pts = float(entry.get("points", 0.0) or 0.0)
        rate = entry.get("rate")
        # A stage that started and got no module as far as running has no figure to
        # report, and every wording below would put a number on that silence.  The
        # 0.0 is the scratch verdict's arithmetic over an empty table, not a rate
        # any module took: "measured 0.00 of its points" is the same sentence a
        # tree that failed every check earns, and it was published over ten runs
        # whose stage 2 broke its copy-in before the first module started.
        if name == "behavioural" and not _reported(entry.get("modules")):
            verdict.notes.append(
                f"stage {name} ran but measured nothing"
                + (f" ({entry['harness_error']})" if entry.get("harness_error")
                   else f" (status={result.status})")
                + "; there is no rate to credit or withhold, and re-running the "
                  "stage is what would produce one")
            continue
        figure = (f"measured {pts:.2f} of its points" if rate is None else
                  f"measured a {float(rate):.4f} weighted pass rate, worth "
                  f"{pts:.2f} points")
        verdict.notes.append(
            f"stage {name} ran and {figure}, but the ladder stopped "
            f"below it ({verdict.blocked_by or 'earlier stage'}); it is not counted"
        )

def _summarise_unscored(result: StageResult | None) -> str:
    """One line about a stage result the ladder is about to discard.

    Reporting only, and defensive on purpose: this runs on the path where the
    submission has already scored 0, and a crash here would turn a decided
    stage-1 failure into a harness error charged to the submission.  Anything
    unreadable degrades to no line rather than to an exception.
    """
    if result is None:
        return ""
    try:
        if result.status != "ok":
            return f"ran but did not complete (status={result.status})"
        units = list(result.units or [])
        checks = list(result.checks or [])
        ok = sum(1 for u in units if u.status == "ok")
        failed = sum(1 for c in checks if c.verdict == "fail")
        errored = sum(1 for c in checks if c.verdict == "error")
        earned, total = pooled(checks)
        rate = (earned / total) if total > 0 else 0.0
        # Pooled flat over every scored check in the result, which is not the
        # figure a module's weight shapes: this line runs before any module is
        # weighed, so calling it weighted would promise a reader that a heavier
        # module moved it.  `verdict.behavioural_rate` is the weighted one.
        return (
            f"ran and completed: {ok}/{len(units)} module(s) ok, "
            f"{len(checks)} check(s), {failed} failed, {errored} errored, "
            f"pass rate {rate:.4f} over its scored checks"
        )
    except Exception as exc:                                  # noqa: BLE001
        return f"ran; this summary could not be built ({type(exc).__name__})"


def _report_behavioural_only(
    result: StageResult | None, verdict: Verdict, policy: ScoringPolicy
) -> None:
    """Attach a measured behavioural result to a verdict the gate already decided.

    The audit gate zeroes a submission, so the ladder stops and stage 2 is
    never graded.  When stage 2 nonetheless RAN -- because the stages are invoked
    separately and stage 1's outcome is not known until both are on disk -- its
    result sits in the results directory unread, and the report told the reader
    "stage 2  behavioural modules ..... NOT RUN" over twelve measured modules.
    That is the one question this report exists to answer: whether a zero was the
    submission's fault or the grader's.  A reader who is told stage 2 never ran
    cannot see that the build succeeded, nor that 88 cases were captured.

    Report-only, and the distinction matters.  Grading the behavioural result into
    the real verdict would let `grade_behavioural`'s error paths set
    `harness_error` -- turning a legitimate policy zero (valid: true, score 0
    because a required gate failed) into an invalid run that has to be re-run.
    A reporting bug is worth strictly less than that.  It would also overwrite
    `blocked_by`, which the stage-3 section reads to name where the ladder
    stopped: "stage 2 did not reach the score stage 3 requires" is a false account
    of a run that stopped at stage 1.

    So the measurement happens against a throwaway verdict and only the
    report-facing fields are copied.  `score`, `valid`, `harness_error` and
    `blocked_by` stay as stage 1 left them.
    """
    if result is None:
        return
    shadow = Verdict(task=verdict.task, max_score=policy.max_score)
    try:
        grade_behavioural(result, shadow, policy)
    except Exception as exc:                                  # noqa: BLE001
        # The same rule as `_record_unscored`'s, for the same reason, and it was
        # missing here.  This function annotates a verdict whose score is already
        # decided, so a stage-2 result it cannot parse must cost the report its
        # module table and nothing else.  Unguarded, the raise escaped `grade()`
        # itself and made a decided policy zero into a crashed run -- the exact
        # outcome the paragraph above says this function exists to avoid, reached
        # by the one route it did not cover: not `grade_behavioural` *assigning*
        # `harness_error`, but `grade_behavioural` raising.  Recorded on the shadow
        # so the branch below says so in the report rather than falling silent.
        shadow.harness_error = f"{type(exc).__name__}: {exc}"
    if not shadow.modules:
        # Present but unreadable, empty, or errored.  Worth saying, since "no
        # module table" and "no stage 2" look identical to a reader, but there is
        # nothing to tabulate.
        #
        # The metadata copy is the point of this branch and not a detail of it.
        # A stage that produced no modules is the case where the report has the
        # least to go on and says the most: with nothing under
        # `metadata["behavioural"]`, `report.py` falls back to naming
        # `blocked_by`, and on these runs that is the audit gate -- so a
        # stage 2 whose copy-in broke its pipe was published as "NOT RUN (the
        # audit gate failed)".  Consistent with the verdict, contradicted by
        # nothing, and an account of a cause the harness never observed.  Ten of
        # the twelve unmeasured runs read exactly that way.  Report-facing, so it
        # keeps this function's contract: `score`, `valid`, `harness_error` and
        # `blocked_by` are still stage 1's.
        stage_meta = dict(getattr(result, "metadata", None) or {})
        if stage_meta:
            verdict.metadata["behavioural"] = stage_meta
        if shadow.harness_error:
            verdict.notes.append(
                f"stage 2 ran but its result could not be read "
                f"({shadow.harness_error}); it was not scored either way, the "
                f"score is 0 by the audit gate")
        return

    # Measurements, not awards.  Three groups of fields, not two, and which group
    # a field is in is decided by what a consumer does with it:
    #
    #   modules  -- a factual record of what executed.  `flatten` keys
    #       `stage2_modules` and every `module_*_rate` off it, and all of those
    #       become CORRECT by copying: the stage ran, and those are its rates.
    #   behavioural_points, behavioural_rate  -- amounts AWARDED, published to
    #       Harbor as `stage2_points` beside `score` and `reward`.  A consumer that
    #       adds the stage points and compares the total to `score` is doing a
    #       reasonable thing, and would find 41.2 + 0.0 != 0.0.  So these stay 0,
    #       and the measured totals travel in metadata for the report to render.
    #   stages_run  -- NOT written here, though an earlier draft of this function
    #       did, on the ground that it too is a factual record.  It reads that way
    #       and is published as a different claim: `flatten` emits BOTH
    #       `stage2_ran` and `stage2_scored` from it, the second alone.  Appending
    #       here says the stage was scored, next to the `stage2_points: 0.0` two
    #       lines above -- which is the contradiction the paragraph above avoids
    #       for points, reintroduced in the field beside them.  Nothing is lost by
    #       leaving it: `stage2_ran` is already `stages_run or unscored_stages`, so
    #       the "it ran" signal arrives by the second half, which is also how
    #       stage 3 -- annotated by `_record_unscored` alone, and never appended --
    #       has always reported the same situation.  Appending would make stage 2
    #       the only stage whose uncredited run is published as a scored one.
    verdict.modules = shadow.modules
    verdict.metadata["behavioural"] = shadow.metadata.get("behavioural", {})
    # The flag the report keys on, and the numbers it prints.  Without them the
    # module table renders exactly like a scored one.
    verdict.metadata["behavioural_measured_not_counted"] = True
    verdict.metadata["behavioural_measured_points"] = round(
        shadow.behavioural_points, 4)
    verdict.metadata["behavioural_measured_rate"] = round(
        shadow.behavioural_rate, 6)
    # No note from here.  There was one -- "stage 2 ran and is reported below for
    # diagnosis only: it measured N point(s) across M module(s), none of which are
    # awarded, because stage 1 stopped the ladder" -- and `_record_unscored`
    # appends its own sentence carrying the same number for the same stage, so the
    # notes block printed the fact twice.  Its is the one kept: it names the
    # `blocked_by` id, it is emitted for stage 3 in the same words, and it is
    # written on every path where this one would be plus the paths where this
    # function bails early.  Everything this sentence added beyond it -- that the
    # figure is a measurement and not an award, and the module count -- is already
    # on the stage-2 header line and in the table underneath it.
    if shadow.blocked_by:
        # Stage 2 would not have carried the ladder either.  Recorded, not
        # promoted to `blocked_by`: the ladder stopped at stage 1, and that is
        # what stopped it.
        verdict.metadata["behavioural_would_have_blocked"] = shadow.blocked_by


def grade(
    task: str,
    policy: ScoringPolicy,
    audit: StageResult | None,
    behavioural: StageResult | None,
    verification: StageResult | None = None,
    *,
    declared: Iterable[str] | None = None,
) -> Verdict:
    """Run the ladder over whatever stage results exist.

    Stages that were correctly not run — because an earlier one stopped the
    ladder — may be ``None``.  ``blocked_by`` records where it stopped, so a
    reader never has to infer it from which fields are zero.

    ``declared`` is the set of stages the task's ``evaluation.toml`` declares, and
    it is what separates a stage that is missing from one that was never asked
    for.  The two production callers each compute the distinction and must pass it:
    dropping it is how a ladder that reaches stage 3 and finds no result publishes
    stage 2's points as a final score.  Left unset it means "grade what I handed
    you": the shape every by-hand invocation of stages 1 and 2 uses, and the shape
    all of this module's report-only shadow passes use.

    A stage result that exists but is not reached is a third case, distinct from
    both: the driver ran it anyway.  It cannot earn a point — the ladder's whole
    purpose is that stage 1 zeroes everything — but it is recorded in ``unscored``
    and ``unscored_stages`` so the report can say "ran and was discarded" instead
    of "NOT RUN", which is a claim about the harness that would be false.

    Recording it buys the submission nothing: the score below is built from
    ``behavioural_points`` and ``verification_points`` only, and neither is touched
    once a rung fails.  ``stages_run`` likewise keeps its meaning — the stages this
    ladder actually climbed — so a reader can tell a stage that counted from one
    that merely ran.
    """
    verdict = Verdict(task=task, max_score=policy.max_score)

    if not grade_audit(audit, verdict):
        # The ladder stops here and the score is 0 either way.  But stage 1 and
        # stage 2 are separate invocations, so stage 2 has often already run by
        # the time this is decided, and its result is the difference between "the
        # submission is broken" and "only stage 1 needs re-running".
        #
        # All three writers, which is what the stage-2 path below already does for
        # the two it has.  They fill different fields for different readers and no
        # one of them subsumes another -- see the block comment above the three
        # definitions.  Four branches fixed "a stage that ran must not be reported
        # NOT RUN" and three arrived here; running only one leaves another's
        # consumer printing NOT RUN over an artifact sitting beside the report.
        #
        # Both are given the same result, and the second is not gated on the
        # first.  It was: `None if reported else behavioural` skipped
        # `_record_unscored` for stage 2 whenever `_report_behavioural_only` had
        # written a module table, on the reading that the richer writer had
        # already covered it.  It had not -- they populate disjoint fields, and
        # only the second fills `unscored["behavioural"]`, which is the one the
        # report reads for the rate/points split, the stage's own reason for
        # stopping, and its notes.  So the gate turned the presence of the better
        # module table into the absence of everything else, leaving the very
        # reader this branch exists for with `uncredited.get("behavioural") == {}`.
        #
        # The fourth branch's writer is not called here and is not missing: it fills
        # the same dict `_record_unscored` does, in a different shape, so it is a
        # `Verdict.uncounted` projection over this one rather than a fifth field.
        _report_behavioural_only(behavioural, verdict, policy)
        _record_unscored(verdict, policy, behavioural, verification)
        for name, res in (("behavioural", behavioural), ("verification", verification)):
            line = _summarise_unscored(res)
            if line:
                verdict.unscored_stages[name] = line
        return verdict

    if not grade_behavioural(behavioural, verdict, policy):
        # Same rung, one stage down: stage 3 may have been run by a driver before
        # stage 2 stopped the ladder, and reporting it absent would be the same
        # false sentence.  `behavioural` is passed as None deliberately -- it was
        # counted here, so it belongs in the scored fields and not in the record of
        # what went uncredited.
        _record_unscored(verdict, policy, None, verification)
        # 0, and written from the field rather than as a literal so this line cannot
        # disagree with what `grade_behavioural` decided.
        verdict.score = verdict.behavioural_points
        line = _summarise_unscored(verification)
        if line:
            verdict.unscored_stages["verification"] = line
        return verdict

    # Reached only when both rungs below passed, so `declared` is the whole
    # question: the ladder is at stage 3 and either the task asked for one or it
    # did not.
    grade_verification(verification, verdict, policy,
                      expected=bool(declared and "verification" in declared))
    verdict.score = verdict.behavioural_points + verdict.verification_points
    if verdict.score > policy.max_score:
        verdict.score = policy.max_score
    return verdict
