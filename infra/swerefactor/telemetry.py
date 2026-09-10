"""What a run's grading models cost in time and tokens.

A score says what the submitted tree does.  It does not say, in one place, what
deciding it cost: stage 1 drives three reviews and stage 3 six rounds, each
recording its own ``usage`` inside ``score.json``'s ``metadata``, so a reader
comparing runs has to walk two nested structures to add up seven models.
``for_run`` projects them into one flat ``telemetry.json`` per run.

Stage 1 and stage 3 are kept in separate lists because a per-run total over both
would average a six-model stage-3 spend into a figure that belongs to none of the
models involved.  What this module reports is cost, never verdicts: a reader who
wants to know what was decided reads ``score.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA = "swerefactor.telemetry/1"

#: The graded verdict, which carries the grading models' ``usage``.
SCORE_JSON = "score.json"
TELEMETRY_JSON = "telemetry.json"


def grading(score: dict[str, Any]) -> dict[str, Any]:
    """The grading models' time and tokens, projected from a ``score.json``.

    Stage 1's three reviews are ``metadata.audit.samples[]`` and stage 3's
    six rounds are ``metadata.verification.rounds[]``; each carries an
    ``elapsed_sec`` and a ``usage`` block the stage's own driver populated.  This
    picks the telemetry fields out and drops the verdicts, citations and prose --
    a telemetry record is about cost, not about what was decided, and a reader
    who wants the decision reads ``score.json``.

    ``credits`` is carried through where the endpoint reported its own billing
    unit (Anthropic does; the OpenAI chat and command drivers do not), and is
    ``None`` otherwise rather than 0, so an absent figure is not read as a free
    round -- the same distinction ``models.py`` makes when it omits a zero
    ``credits`` from ``usage``.
    """
    md = score.get("metadata") or {}
    return {
        "stage1": [_sample(s) for s in (md.get("audit") or {}).get("samples") or []],
        "stage3": [_round(r) for r in (md.get("verification") or {}).get("rounds") or []],
    }


def _sample(sample: dict[str, Any]) -> dict[str, Any]:
    usage = sample.get("usage") or {}
    return {
        "index": sample.get("index"),
        "driver": usage.get("driver"),
        "model": usage.get("model"),
        "elapsed_sec": sample.get("elapsed_sec"),
        "calls": usage.get("calls"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "credits": usage.get("credits"),
    }


def _round(record: dict[str, Any]) -> dict[str, Any]:
    usage = record.get("usage") or {}
    return {
        "adversary": record.get("adversary"),
        "driver": usage.get("driver"),
        "model": usage.get("model"),
        "outcome": record.get("outcome"),
        "elapsed_sec": record.get("elapsed_sec"),
        "calls": usage.get("calls"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "credits": usage.get("credits"),
    }


def for_run(run_dir: str | Path) -> dict[str, Any]:
    """One run's grading telemetry.

    ``run_dir`` is a ``<task>/<model>/<run>`` directory holding ``score.json``.
    ``task`` is taken from the verdict, which names its own task, and
    ``model``/``run`` from the directory, so a telemetry row and the score beside
    it name the same run.
    """
    run_dir = Path(run_dir).resolve()
    score_path = run_dir / SCORE_JSON
    if not score_path.exists():
        raise FileNotFoundError(f"no {SCORE_JSON} in {run_dir}")
    score = json.loads(score_path.read_text(encoding="utf-8"))

    return {
        "schema": SCHEMA,
        "task": score.get("task") or run_dir.parent.parent.name,
        "model": run_dir.parent.name,
        "run": run_dir.name,
        **grading(score),
    }


def write(run_dir: str | Path) -> Path:
    """Write ``telemetry.json`` into ``run_dir`` and return its path."""
    run_dir = Path(run_dir).resolve()
    out = run_dir / TELEMETRY_JSON
    out.write_text(json.dumps(for_run(run_dir), indent=2, sort_keys=False) + "\n",
                   encoding="utf-8")
    return out
