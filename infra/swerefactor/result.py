"""The one result contract every evaluation stage writes.

Three stages produce results — the agentic audit gate, the behavioural
modules, the verification probe — and they are as different as an LLM audit is
from a jar-entry diff.  They still write the same envelope, because ``score.py``
has to read all three without knowing which is which, and because a human
reading two reports side by side should not have to learn two formats.

The envelope::

    {
      "schema": "swerefactor.stage-result/1",
      "stage": "audit" | "behavioural" | "verification",
      "task": "lang01-cmark-c-to-rust",
      "status": "ok" | "error",             # did the stage RUN, not did it pass
      "started_at": "2026-07-30T09:00:00Z",
      "duration_sec": 41.2,
      "units": [ ... ],                     # modules / audits / probe rounds
      "checks": [ ... ],                    # the atomic verdicts
      "metadata": { ... },                  # per-stage, free-form
      "notes": [ "..." ]
    }

``status`` and the verdicts are deliberately separate axes.  "the stage crashed"
and "the submission failed" are different facts and a scorer that conflates them
turns every infrastructure outage into a zero.

Two escape hatches keep this shape usable for stages that do not exist yet:
``metadata`` on the envelope, and ``metadata`` on every check and unit.  The
required keys are the ones the scorer reads; anything else a stage wants to
record about itself goes in there and travels through untouched.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

SCHEMA = "swerefactor.stage-result/1"

Stage = Literal["audit", "behavioural", "verification"]
STAGES: tuple[str, ...] = ("audit", "behavioural", "verification")

#: A check either held, did not hold, or could not be decided.  ``error`` is not
#: a synonym for ``fail``: a module that crashed before it could look at the
#: submission has not established anything, and a report that says so is worth
#: more than one that silently scores it as a defect.  Scoring policy decides how
#: to treat it (behavioural: as a miss; audit: as a stage error).
Verdict = Literal["pass", "fail", "error", "skip"]
VERDICTS: tuple[str, ...] = ("pass", "fail", "error", "skip")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Check:
    """One atomic verdict.

    ``id`` is stable across runs and unique within a stage: the report diffs two
    runs by it, so a check whose id moves every run makes the diff useless.

    ``weight`` is one bit on a check and one only: zero means *do not record this
    check*.  A behavioural module divides its own weight equally among the checks
    it records, so 0.4 and 1.0 are the same vote and a weight-0 row is dropped at
    collection (``behavioural.py:_collect``) rather than carried as an exemption.
    Audit checks ignore weight entirely — a gate does not have a pass rate.

    ``required`` belongs to the audit stage, where it marks a criterion that
    fails the gate on its own.  It has no meaning inside a behavioural module: the
    stage is all-or-nothing, so a single failing check already costs everything a
    module-wide veto could, and "nothing was built" is expressed by the checks
    about the artifact each reporting ``error``, which they already do.

    ``evidence`` is where an audit says *why*.  Grounded evidence (see
    ``evidence.py``) carries ``path``/``line`` that the runner then confirms
    really exists, which is what stops a model from citing a file it invented.
    """

    id: str
    verdict: Verdict
    summary: str = ""
    weight: float = 1.0
    required: bool = False
    unit: str = ""
    detail: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    duration_sec: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.verdict == "pass"

    @property
    def judged(self) -> bool:
        """Whether this check reached a verdict about the submission at all.

        Named for what it observes, not for what it decides: it does **not**
        control the denominator.  ``pooled`` charges a skip, so a check that did
        not run costs its module credit exactly as a failure does — a property
        called ``scored`` would promise it selects the scored checks, which is a
        trap for the next reader.  What it is good for is reporting: "N checks
        did not run" is worth printing next to a rate, and it is the difference
        between a module that answered badly and one that answered nothing.

        The reason a skip is charged is that the environment is fixed and
        offline, so nothing here skips for environmental reasons: a skip means
        the artifact the check reads was never produced, which is the
        submission's miss.  Measured on the 520-cell corpus: every one of the
        4368 skips was either an unanswerable question, deleted from its suite
        outright, or a check some other model passed — never a case where the
        question existed and the submission was blameless.  ``srb_skip_ok``
        licenses the *wording* of a skip, not an exemption from being counted.
        """
        return self.verdict != "skip"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "verdict": self.verdict,
            "summary": self.summary,
            "weight": round(float(self.weight), 6),
        }
        if self.required:
            out["required"] = True
        if self.unit:
            out["unit"] = self.unit
        if self.detail:
            out["detail"] = self.detail
        if self.evidence:
            out["evidence"] = self.evidence
        if self.duration_sec is not None:
            out["duration_sec"] = round(self.duration_sec, 3)
        if self.metadata:
            out["metadata"] = self.metadata
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Check":
        verdict = str(raw.get("verdict", "error")).lower()
        if verdict not in VERDICTS:
            # An unknown verdict is a broken producer, not a pass.
            verdict = "error"
        try:
            weight = float(raw.get("weight", 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        evidence = raw.get("evidence") or []
        if not isinstance(evidence, list):
            evidence = [{"note": str(evidence)}]
        return cls(
            id=str(raw.get("id") or raw.get("name") or "<unnamed>"),
            verdict=verdict,  # type: ignore[arg-type]
            summary=str(raw.get("summary", "")),
            weight=max(0.0, weight),
            required=bool(raw.get("required", False)),
            unit=str(raw.get("unit", "")),
            detail=str(raw.get("detail", "")),
            evidence=[e if isinstance(e, dict) else {"note": str(e)} for e in evidence],
            duration_sec=_maybe_float(raw.get("duration_sec")),
            metadata=raw.get("metadata") or {},
        )


def _maybe_float(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass
class Unit:
    """A named group of checks: one behavioural module, one audit area, one probe.

    ``weight`` is the group's share of its stage.  ``status`` records whether the
    group *ran*; a module whose command exited non-zero before writing anything
    is ``error`` with zero checks, and the scorer credits it nothing rather than
    dividing by zero.

    ``unreached`` is the one status a module never writes about itself.  The suite
    seeds every declared module with it before the first one starts and replaces
    each as it finishes, so the result on disk always describes the whole suite --
    which is what makes a stage killed at its own timeout legible: the modules that
    ran carry their real numbers and the rest say, in a word, that the stage clock
    ran out before they were reached.  Without it those modules were simply absent,
    and an absent module leaves the denominator to be assembled from whatever
    happened to finish.
    """

    id: str
    title: str = ""
    weight: float = 1.0
    status: Literal["ok", "error", "timeout", "skip", "unreached"] = "ok"
    summary: str = ""
    duration_sec: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "weight": round(float(self.weight), 6),
            "status": self.status,
        }
        if self.summary:
            out["summary"] = self.summary
        if self.duration_sec is not None:
            out["duration_sec"] = round(self.duration_sec, 3)
        if self.metadata:
            out["metadata"] = self.metadata
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Unit":
        status = str(raw.get("status", "ok")).lower()
        if status not in ("ok", "error", "timeout", "skip", "unreached"):
            status = "error"
        try:
            weight = float(raw.get("weight", 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        return cls(
            id=str(raw.get("id") or "<unnamed>"),
            title=str(raw.get("title", "")),
            weight=max(0.0, weight),
            status=status,  # type: ignore[arg-type]
            summary=str(raw.get("summary", "")),
            duration_sec=_maybe_float(raw.get("duration_sec")),
            metadata=raw.get("metadata") or {},
        )


def _harness_stamp() -> dict[str, Any]:
    """This harness's identity, or ``{}`` if it cannot describe itself.

    Never raises.  A stage that has finished grading must be able to write its
    result; failing that write to record provenance would turn a graded run into
    a harness error, which is a strictly worse outcome than an unattributed one.
    """
    try:
        import swerefactor

        return {"fingerprint": swerefactor.fingerprint()}
    except Exception:  # pragma: no cover - provenance must not break grading
        return {}


@dataclass
class StageResult:
    """One stage's complete output — the file ``score.py`` reads."""

    stage: Stage
    task: str
    status: Literal["ok", "error"] = "ok"
    started_at: str = field(default_factory=utcnow)
    duration_sec: float = 0.0
    units: list[Unit] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: Which harness produced this, stamped once when the result is constructed.
    #:
    #: A field and not a property, because ``to_dict`` must not recompute it: a
    #: result read off disk and written back out would then claim to have been
    #: graded by whatever is running now, which is the one thing a provenance
    #: record must never say.  ``from_dict`` copies whatever was recorded, empty
    #: included -- a result written before this existed has no harness to name,
    #: and inventing one is worse than admitting that.
    harness: dict[str, Any] = field(default_factory=lambda: _harness_stamp())

    # -- construction helpers ------------------------------------------------

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    def unit(self, unit_id: str) -> Unit | None:
        for u in self.units:
            if u.id == unit_id:
                return u
        return None

    def checks_of(self, unit_id: str) -> list[Check]:
        return [c for c in self.checks if c.unit == unit_id]

    def note(self, text: str) -> None:
        self.notes.append(text)

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        # `harness` is the grader's own identity, not the stage's free-form
        # metadata, so it gets its own key: a task may write anything into
        # `metadata` and provenance must not be something a task can overwrite.
        return {
            "schema": SCHEMA,
            "stage": self.stage,
            "task": self.task,
            "status": self.status,
            "started_at": self.started_at,
            "duration_sec": round(self.duration_sec, 3),
            "harness": self.harness,
            "units": [u.to_dict() for u in self.units],
            "checks": [c.to_dict() for c in self.checks],
            "metadata": self.metadata,
            "notes": self.notes,
        }

    def write(self, path: str | Path) -> Path:
        """Write atomically: a half-written result read by the scorer is a zero
        for a reason that has nothing to do with the submission."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, indent=2, sort_keys=False)
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
    def from_dict(cls, raw: dict[str, Any]) -> "StageResult":
        stage = str(raw.get("stage", "")).lower()
        if stage not in STAGES:
            raise ValueError(
                f"unknown stage {stage!r}; expected one of {', '.join(STAGES)}"
            )
        status = str(raw.get("status", "ok")).lower()
        return cls(
            stage=stage,  # type: ignore[arg-type]
            task=str(raw.get("task", "")),
            status="ok" if status == "ok" else "error",
            started_at=str(raw.get("started_at") or utcnow()),
            duration_sec=float(raw.get("duration_sec") or 0.0),
            units=[Unit.from_dict(u) for u in raw.get("units") or []],
            checks=[Check.from_dict(c) for c in raw.get("checks") or []],
            metadata=raw.get("metadata") or {},
            notes=[str(n) for n in raw.get("notes") or []],
            harness=raw.get("harness") or {},
        )

    @classmethod
    def read(cls, path: str | Path) -> "StageResult":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    @classmethod
    def failed(cls, stage: Stage, task: str, reason: str) -> "StageResult":
        """A stage that could not run at all, in the same shape as one that did."""
        res = cls(stage=stage, task=task, status="error")
        res.note(reason)
        res.metadata["error"] = reason
        return res


def pooled(checks: Iterable[Check]) -> tuple[float, float]:
    """Passing and scored check COUNTS over ``checks`` -- every check counts once.

    Checks inside a module carry no relative weight: a module's weight is declared
    once in ``suite.toml`` and its checks divide that share equally.  So this counts
    checks rather than summing ``weight``, and the second element is a denominator
    of checks, which is what makes a module row readable as ``passed/total`` with no
    scoring formula in between.

    ``weight`` on a Check carries one bit and one only: zero means *this check does
    not score*.  Those are the observation checks -- a module reporting what it
    found without judging it -- and they are the only thing that leaves the
    denominator, because a check that cannot be failed must not be able to dilute
    one that can.  Any positive weight is one scored check; 0.4 and 1.0 are the
    same vote.

    A **skip stays in and scores 0**.  A skip is not evidence that the submission
    is right, and the same question is asked of every submission: answered by some,
    and unreachable in the others because they built nothing for it to read.
    Letting a skip leave would pay a submission for the gap it created -- shrinking
    its own denominator until 4 of 5 checks read 1.0000 -- and would make a task's
    denominator differ per model, so two cells' module rows would not be comparable.
    A question the *reference* cannot answer is settled by deleting it from the
    suite, which is the only honest place to settle it, and not by a verdict that
    costs nothing.

    Returned as floats, not ints, so every caller that divides them or writes them
    into JSON reads one number for the pool and one for the share of it.
    """
    passed = total = 0
    for c in checks:
        if c.weight <= 0:
            continue
        total += 1
        if c.ok:
            passed += 1
    return float(passed), float(total)


def rate(checks: Iterable[Check]) -> float:
    """Equal-weight pass rate; a pool with no scoring check in it rates 0.0.

    Not 1.0: an empty pool means the checks never ran, and "nothing failed"
    is not evidence that anything worked.  An all-skipped module reaches 0.0 the
    ordinary way now -- ``pooled`` counts its skips and none of them passed --
    rather than through this guard.
    """
    passed, total = pooled(checks)
    return (passed / total) if total > 0 else 0.0
