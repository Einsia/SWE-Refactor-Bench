"""Readers for the three TOML files that describe how a task is graded.

    tests/evaluation.toml            orchestration + scoring policy
    tests/behavioural/suite.toml      the behavioural module manifest
    tests/verification/probe.toml     the verification round

They share a house style, which is the point of putting them in one module:

* ``schema`` names the contract, so a task written against an older shape is
  rejected with a sentence instead of a ``KeyError`` three frames deep.
* every table accepts a ``metadata`` sub-table that this loader carries through
  without interpreting.  Tasks differ — one needs a JDK version recorded, another
  needs the ISA baseline — and the alternative to an escape hatch is a required
  key that means nothing for three tasks out of four.
* defaults live here, once, rather than in twenty copies of a task file.

Validation is strict about every key it reads and about every key it does not:
a table that is not a declared bag rejects a name this reader would ignore,
because ``raw.get(key, default)`` cannot tell an omitted key from a misspelled
one and the default it returns is a working run of the wrong policy.  It reports
*all* the problems in a file rather than the first, because a task author fixing
a manifest wants the list.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from . import tomlcompat

EVALUATION_SCHEMA = "swerefactor.evaluation/1"
SUITE_SCHEMA = "swerefactor.behavioural-suite/1"
#: Stage 1's read-only scan.  A separate schema for the same ``Suite`` shape,
#: because the two suites answer to different rules -- a behavioural module builds
#: and runs the submission, a scan module may only read it -- and a file that
#: declared the wrong one would be run under the wrong contract.
SCAN_SCHEMA = "swerefactor.scan-suite/1"
PROBE_SCHEMA = "swerefactor.verification-probe/1"


class ConfigError(Exception):
    """A task's own configuration is wrong — an authoring bug, not a submission
    failure.  Callers turn this into a loud abort, never into a score of zero."""


def _require(raw: dict[str, Any], key: str, where: str) -> Any:
    if key not in raw:
        raise ConfigError(f"{where}: missing required key {key!r}")
    return raw[key]


def _read_toml(path: str | Path) -> dict[str, Any]:
    """``tomlcompat.load``, with a malformed file reported as a ``ConfigError``.

    The distinction the loaders make everywhere else -- a wrong task file raises
    ``ConfigError``, and everything else is a bug in the harness -- was not being
    made for the file's own syntax: ``tomlcompat.load`` raises ``ValueError``, and
    the callers of these loaders catch ``ConfigError``.  So a missing ``]]``
    escaped every handler in ``cli.py``, printed a traceback, and exited 1.  Exit
    1 is this CLI's "the run failed"; a task file that does not parse is exit 2,
    and the stage still has to leave a result file behind saying so.  Both were
    lost to one uncaught type.

    ``FileNotFoundError`` deliberately still propagates.  ``cmd_audit``
    catches it alongside ``ConfigError`` and reports the path, and
    ``behavioural.py`` distinguishes an absent module directory from an unreadable
    one -- turning it into ``ConfigError`` here would flatten that.
    """
    try:
        return tomlcompat.load(path)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _number(raw: dict[str, Any], key: str, default: float, where: str) -> float:
    """A float from file data, or a ``ConfigError`` naming the key that is wrong.

    ``float(raw.get(...))`` reads well and fails badly: on ``timeout_sec = "600"``
    it raises ``ValueError`` from inside a dataclass constructor, which is not a
    ``ConfigError``, so it left the same traceback-and-exit-1 as a syntax error --
    and the message named neither the file nor the key, only the string that
    would not convert.

    Booleans are rejected rather than coerced.  ``True`` is a perfectly good float
    in Python, so ``verification_models = true`` would have configured one model
    and said nothing; TOML has a bool type of its own and a task that typed one
    here meant something else.
    """
    if key not in raw:
        return float(default)
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: {key} must be a number, not "
                          f"{type(value).__name__} ({value!r})")
    return float(value)


def _bool(raw: dict[str, Any], key: str, default: bool, where: str) -> bool:
    """A bool from file data, or a ``ConfigError`` naming the key that is wrong.

    ``bool(raw.get(key, default))`` is the shape this replaces.  It reads as a
    coercion and behaves as a truth test on the object, so every non-empty string
    is ``True``: ``required = "false"`` loaded as *required*, and
    ``network_allowed = "false"`` opened the network on a task that had written it
    shut.  Quoting a bool is a plausible slip in a format whose bools are bare
    words, and it is the one wrong value that inverts the field instead of
    breaking it -- ``"true"`` would at least have been right by accident.

    TOML has a bool type.  A value here that is not one was meant as something
    else, so it is named rather than interpreted.
    """
    if key not in raw:
        return bool(default)
    value = raw[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: {key} must be true or false, not "
                          f"{type(value).__name__} ({value!r})")
    return value


def _exit_codes(raw: dict[str, Any], key: str, where: str) -> list[int]:
    """A list of process exit codes, or a ``ConfigError`` naming what is wrong.

    Three ways to get this wrong, and the first is why the check exists rather than
    a comprehension.  ``0`` in this list makes every *passing* run an
    infrastructure fault, so no candidate can ever discriminate and all six rounds
    survive: the full sixty points, paid to any submission, from one number in a
    file nobody would re-read.  ``1`` does the mirror -- every failing candidate
    becomes a fault -- and costs nothing but decides nothing either.  Both are
    refused by name.

    The rest is ordinary: a code has to be a whole number in the range a process
    can actually exit with, and ``true`` is not a code even though Python would
    happily index a list with it.
    """
    if key not in raw:
        return []
    value = raw[key]
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"{where}: {key} must be a list of exit codes, not "
                          f"{type(value).__name__} ({value!r})")
    codes: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ConfigError(f"{where}: {key} contains {item!r}, which is not an "
                              f"exit code")
        if not 0 <= item <= 255:
            raise ConfigError(f"{where}: {key} contains {item}, and a process "
                              f"cannot exit with it (0-255)")
        if item in (0, 1):
            raise ConfigError(
                f"{where}: {key} contains {item}, which is the candidate's own "
                f"verdict rather than a fault -- 0 is a candidate that passed and 1 "
                f"is one whose assertions failed. Treating "
                f"{'a pass' if item == 0 else 'a failure'} as an infrastructure "
                f"fault would make every candidate undecidable and every round a "
                f"survival")
        codes.append(item)
    return codes


def _check_keys(raw: dict[str, Any], known: tuple[str, ...], where: str,
                *, bag: bool = False) -> None:
    """Reject keys this reader does not read.

    Every loader here reaches for its keys with ``raw.get(key, default)``, which
    cannot tell a key that was omitted from one that was misspelled.  So
    ``behavioural_gate_mn = 65`` loaded clean and graded against the default 70,
    ``wieght = 0.26`` normalised as an ordinary module of weight 1.0, and
    ``requried = true`` left a gate advisory.  Each of those is a silent change
    to the ladder, written down in the task's own file, that no stage can detect
    afterwards -- the run looks like a run of a task that meant the default.

    ``bag=True`` is for the two tables that deliberately keep their extra keys:
    ``[stages.*]`` hands ``image``, ``samples`` and the rest to a runner, and
    ``[[adversary]]`` hands ``driver_options`` to a driver, so an unknown key
    there is the feature.  What is *not* the feature is a near-miss of a key this
    reader does read -- ``timeout_secs`` in a bag lands in ``options``, where
    nothing reads it, and the stage runs on the default hour.  Those are named
    with the same suggestion, and the 14 option keys the shipped tasks use are
    none of them within reach of one.
    """
    extra = [k for k in raw if k not in known]
    if bag:
        extra = [k for k in extra
                 if difflib.get_close_matches(k, known, n=1, cutoff=0.8)]
    if not extra:
        return
    problems = []
    for k in sorted(extra):
        near = difflib.get_close_matches(k, known, n=1, cutoff=0.6)
        problems.append(f"{k!r}" + (f" (did you mean {near[0]!r}?)" if near else ""))
    raise ConfigError(
        f"{where}: unknown key(s) {', '.join(problems)}; this reader reads "
        f"{', '.join(sorted(known))}"
    )


def _check_schema(raw: dict[str, Any], expected: str, where: str) -> None:
    got = raw.get("schema")
    if got is None:
        raise ConfigError(f"{where}: missing 'schema' (expected {expected!r})")
    if got != expected:
        raise ConfigError(f"{where}: schema is {got!r}, this reader speaks {expected!r}")


# --------------------------------------------------------------------------- #
# Scoring policy
# --------------------------------------------------------------------------- #


@dataclass
class ScoringPolicy:
    """The three-stage ladder, with the published numbers as defaults.

    The whole score is::

        stage 1 passed  x  (behavioural_points  +  points_per_survived_model x survivors)

    with the behavioural term paid only when stage 2 passed *every* scored check,
    and the verification term reachable only through that same condition.

    So each stage answers one question and answers it yes or no.  Stage 1 is a
    gate: fail it and the score is 0, whatever the rest would have said.  Stage 2
    is worth ``behavioural_points``, all of it or none -- a single failing check
    pays the same as a tree that never compiled.  Stage 3 is asked only of a
    submission that took stage 2 in full, and pays
    ``points_per_survived_model`` for every adversary that failed to break it.

    Two consequences worth stating, because both are deliberate.  The reachable
    totals are a short list -- 0, ``behavioural_points``, and that plus one step
    per survivor -- so a published score says which rungs a submission reached
    rather than how much of a suite it happened to pass.  And nothing prices a
    partial port: the stage that measures how much works still reports its rate,
    but the rate buys nothing.  A port is finished or it is not.

    Asking stage 3 only of a finished port is also what makes it affordable: six
    models spend an hour each hunting for a behavioural difference, and asking
    them to do that to a tree stage 2 already found differences in spends the
    budget confirming something measured.

    The numbers are configurable per task because a task may want a different
    ladder, but the defaults are the benchmark's published policy and a task that
    changes them says so in its own file where a reader will see it.
    """

    max_score: float = 100.0
    behavioural_points: float = 40.0
    verification_points: float = 60.0
    verification_models: int = 6
    points_per_survived_model: float = 10.0
    # A module that did not run scores zero and keeps its weight, so breaking a
    # module is never cheaper than failing it.  Fixed here rather than made
    # configurable: the only other setting would mean "a module that crashed
    # costs nothing", which rewards sabotage, and a knob whose second position
    # no task may use is worse than no knob.
    metadata: dict[str, Any] = field(default_factory=dict)

    #: Not a bag.  Every number here is cross-checked against another below, so a
    #: typo is a rejected file rather than a silently installed default.  Keys are
    #: rejected rather than ignored for the same reason: a task declaring one this
    #: tuple does not list is a task expecting a rule the grader does not have.
    KNOWN: ClassVar[tuple[str, ...]] = (
        "max_score", "behavioural_points", "verification_points",
        "verification_models", "points_per_survived_model", "metadata",
    )

    @classmethod
    def from_dict(cls, raw: dict[str, Any], where: str) -> "ScoringPolicy":
        _check_keys(raw, cls.KNOWN, f"{where} [scoring]")
        models_raw = _number(raw, "verification_models", 6, where)
        if models_raw != int(models_raw):
            raise ConfigError(f"{where}: verification_models must be a whole "
                              f"number, not {models_raw!r}")
        pol = cls(
            max_score=_number(raw, "max_score", 100.0, where),
            behavioural_points=_number(raw, "behavioural_points", 40.0, where),
            verification_points=_number(raw, "verification_points", 60.0, where),
            # Rejected above rather than truncated here.  `int(6.5)` is 6, so a
            # task that declared 6.5 adversaries would have been graded against
            # six and told nothing -- and `verification_models` is what the
            # per-model payout is checked against, so the discarded half point
            # comes out of the submission's ceiling.
            verification_models=int(models_raw),
            points_per_survived_model=_number(
                raw, "points_per_survived_model", 10.0, where),
            metadata=raw.get("metadata") or {},
        )
        problems = []
        if pol.behavioural_points <= 0:
            problems.append("behavioural_points must be > 0")
        if pol.verification_models < 0:
            problems.append("verification_models must be >= 0")
        budget = pol.points_per_survived_model * pol.verification_models
        if abs(budget - pol.verification_points) > 1e-6:
            problems.append(
                f"verification_points ({pol.verification_points}) does not equal "
                f"points_per_survived_model x verification_models ({budget})"
            )
        if abs(pol.behavioural_points + pol.verification_points - pol.max_score) > 1e-6:
            problems.append(
                f"behavioural_points + verification_points "
                f"({pol.behavioural_points + pol.verification_points}) != max_score "
                f"({pol.max_score})"
            )
        if problems:
            raise ConfigError(f"{where} [scoring]: " + "; ".join(problems))
        return pol


# --------------------------------------------------------------------------- #
# tests/evaluation.toml
# --------------------------------------------------------------------------- #


@dataclass
class Gate:
    """One named question the audit review has to answer.

    Gates are declared here, not only in the prompt, for one reason: the scorer
    has to know which of them are mandatory, and working that out by reading the
    prompt's prose would mean deciding whether a submission scores zero by
    parsing English.  So the prompt argues the question and this file records its
    id and its weight in the ladder.

    ``required`` gates are the hard gate -- one of them failing is the zero.  A
    gate that is not required is reported and reasoned about but does not block:
    the place for an observation that is worth knowing and not worth voiding a
    submission over.
    """

    id: str
    title: str = ""
    #: The question, in one or two sentences, rendered into the prompt.  Written
    #: as something answerable from the code rather than as a rule to match.
    question: str = ""
    required: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    KNOWN: ClassVar[tuple[str, ...]] = ("id", "title", "question", "required",
                                        "metadata")

    @classmethod
    def from_dict(cls, raw: dict[str, Any], where: str) -> "Gate":
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: each [[stages.audit.gate]] must be a table")
        gid = str(_require(raw, "id", f"{where} [[gate]]"))
        if not gid.strip() or " " in gid:
            raise ConfigError(f"{where}: gate id {gid!r} must be a bare token")
        _check_keys(raw, cls.KNOWN, f"{where} [[gate]] {gid}")
        return cls(
            id=gid,
            title=str(raw.get("title", gid)),
            question=str(raw.get("question", "")),
            # A misspelled `required` used to leave a gate advisory, and a quoted
            # `"false"` used to make one mandatory.  Both are the hard gate.
            required=_bool(raw, "required", True, f"{where} [[gate]] {gid}"),
            metadata=raw.get("metadata") or {},
        )


@dataclass
class StageConfig:
    """How one stage is invoked and where it leaves its result."""

    name: str
    enabled: bool = True
    runner: str = ""
    result: str = ""
    timeout_sec: float = 3600.0
    #: Stage-specific settings: the verifier's sample count, the suite path, the
    #: probe's model list.  Kept as a bag so a new stage needs no loader change.
    options: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Only the audit stage uses these; declared as ``[[stages.audit.gate]]``.
    gates: list[Gate] = field(default_factory=list)

    #: A ``ClassVar`` rather than a field.  As a field it was in ``__init__``,
    #: ``__eq__`` and ``repr`` -- the loader's own key list travelling inside
    #: every stage object, and printed by anything that repr'd one.
    KNOWN: ClassVar[tuple[str, ...]] = (
        "enabled", "runner", "result", "timeout_sec", "metadata", "gate",
    )

    @classmethod
    def from_dict(cls, name: str, raw: dict[str, Any], where: str) -> "StageConfig":
        if not isinstance(raw, dict):
            raise ConfigError(f"{where} [stages.{name}]: expected a table")
        # A bag: the keys below are the runner's, and this reader carries them
        # without reading them.  Only a near-miss of a key it *does* read is an
        # error -- landing `timeout_secs` in `options` runs the stage on the
        # default hour and reports nothing.
        _check_keys(raw, cls.KNOWN, f"{where} [stages.{name}]", bag=True)
        options = {k: v for k, v in raw.items() if k not in cls.KNOWN}
        entries = raw.get("gate") or []
        if not isinstance(entries, list):
            raise ConfigError(
                f"{where} [stages.{name}]: gate must be a list of tables, written "
                f"[[stages.{name}.gate]]"
            )
        gates = [Gate.from_dict(e, f"{where} [stages.{name}]") for e in entries]
        ids = [g.id for g in gates]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ConfigError(
                f"{where} [stages.{name}]: duplicate gate id(s): {', '.join(dupes)}"
            )
        if name == "audit" and gates and not any(g.required for g in gates):
            raise ConfigError(
                f"{where} [stages.audit]: no gate is required, so nothing can "
                f"fail the hard gate — mark at least one required = true"
            )
        return cls(
            name=name,
            enabled=_bool(raw, "enabled", True, f"{where} [stages.{name}]"),
            runner=str(raw.get("runner", "")),
            result=str(raw.get("result", f"{name}.json")),
            timeout_sec=_number(raw, "timeout_sec", 3600.0,
                                f"{where} [stages.{name}]"),
            options=options,
            metadata=raw.get("metadata") or {},
            gates=gates,
        )


#: The harness each model family is driven with.  Two clients cover the suite:
#: codex-cli 0.146.0 for the gpt family, and Claude Code 2.1.220 for every other
#: family (claude, deepseek, glm, kimi, qwen).  Claude Code speaks the Anthropic
#: protocol and drives each non-gpt family through the model gateway, so one
#: client -- rather than one per vendor -- is the pin, and a model family is no
#: longer paired with a client built only for it.
#:
#: A run measures a model and a client together -- what a tool call records,
#: whether a round has a token account, and whether a stalled model gets nudged
#: are the client's properties, not the model's -- so the client has to be held
#: still.  The earlier corpus drove kimi through codex over a Responses-to-Chat
#: shim, and the shim's own defects -- a dropped `reasoning` field, a fabricated
#: model-metadata warning -- landed in the model's column.  Driving every
#: non-gpt family through one client removes that whole class of attribution
#: error, and the report still says which client drove each row.
PINNED_HARNESSES: dict[str, str] = {
    "gpt": "codex-cli 0.146.0",
    "claude": "claude-code 2.1.220",
    "deepseek": "claude-code 2.1.220",
    "glm": "claude-code 2.1.220",
    "kimi": "claude-code 2.1.220",
    "qwen": "claude-code 2.1.220",
}

#: The suite default, and what all twenty tasks declare: the gpt family's pin is
#: also the client the archived corpus was driven with.  A task declares one
#: harness because a task file is static, while the client is chosen per run --
#: see `harness_for_model`.
PINNED_HARNESS = PINNED_HARNESSES["gpt"]

#: Every pinned spelling, for membership tests.  A task may declare any of them.
PINNED_HARNESS_VALUES: frozenset[str] = frozenset(PINNED_HARNESSES.values())


def harness_for_model(model: str) -> str | None:
    """The pinned harness for ``model``, by family prefix, or ``None`` if unknown.

    Families are read off the front of the model name -- `gpt-5.6-sol` is gpt,
    `claude-opus-5` is claude, `kimi-k3` is kimi -- because that is the part of a
    model name that is stable across a vendor's releases.  An unknown family
    returns ``None`` rather than the default: guessing a family would report the
    wrong pin as satisfied, and a model this map has not been taught is one whose
    client has to be decided rather than assumed.
    """
    name = (model or "").strip().lower()
    if not name:
        return None
    # Longest prefix first, so a future "gpt-oss" style family that is a prefix of
    # another cannot be shadowed by the shorter key.
    for family in sorted(PINNED_HARNESSES, key=len, reverse=True):
        if not name.startswith(family):
            continue
        rest = name[len(family):]
        # The boundary may not be a letter, and is not always a hyphen: the shipped
        # names include `qwen3-max` and `glm-4.6`, so a digit ends a family name as
        # surely as a separator does.  Requiring a hyphen missed `qwen3-max` and read
        # it as no family at all, which downgrades the per-family check to a
        # membership test on exactly the models most likely to be added next.
        # Requiring only a prefix would make `gptzilla` a gpt.
        if not rest or not rest[0].isalpha():
            return PINNED_HARNESSES[family]
    return None


@dataclass
class AgentPhase:
    """``[agent]`` in ``task.toml`` — what the submission agent is given.

    This block was documented as Harbor's and so nothing here read it, which made
    ``timeout_sec`` inert: the first corpus declared budgets from 6h to 30h while
    each driver ran with whatever flag it was launched with, and one task got 3h
    of a declared 30h, stopped mid-port and scored 0.0 — a figure that reads
    exactly like a model that cannot do the task.  A declaration nothing reads is
    worse than no declaration, because it looks like a control.
    """

    harness: str = PINNED_HARNESS
    #: Wall clock the driver must be launched with, in seconds.
    timeout_sec: float = 0.0
    network_mode: str = ""
    user: str = ""

    #: ``ClassVar`` for the same reason as ``StageConfig.KNOWN``, and exactly the
    #: four keys the shipped tasks write: this block has no bag, so a key that is
    #: not read is not passed on to anything either.
    KNOWN: ClassVar[tuple[str, ...]] = (
        "harness", "timeout_sec", "network_mode", "user",
    )

    @classmethod
    def from_dict(cls, raw: dict[str, Any], where: str) -> "AgentPhase":
        if not isinstance(raw, dict):
            raise ConfigError(f"{where} [agent]: expected a table")
        # `timeout_secs` here would load clean and the run would take the driver's
        # flag, which is the failure this block exists to close.
        _check_keys(raw, cls.KNOWN, f"{where} [agent]")
        timeout = raw.get("timeout_sec")
        if timeout is not None and (isinstance(timeout, bool)
                                    or not isinstance(timeout, (int, float))):
            raise ConfigError(f"{where} [agent]: timeout_sec must be a number")
        if timeout is not None and float(timeout) <= 0:
            raise ConfigError(
                f"{where} [agent]: timeout_sec is {float(timeout):g}; it is the "
                f"clock the run is given, so it has to be positive")
        return cls(
            harness=str(raw.get("harness", "")),
            timeout_sec=float(timeout or 0.0),
            network_mode=str(raw.get("network_mode", "")),
            user=str(raw.get("user", "")),
        )

    @property
    def problems(self) -> list[str]:
        """What is wrong with this block, as sentences a task author can act on."""
        found: list[str] = []
        if not self.harness:
            found.append(
                f"[agent] declares no harness; the suite pins one client per model "
                f"family and a run that does not say which one drove it cannot be "
                f"compared with one that does. The default is {PINNED_HARNESS!r}")
        elif self.harness not in PINNED_HARNESS_VALUES:
            pinned = ", ".join(sorted(PINNED_HARNESS_VALUES))
            found.append(
                f"[agent] harness is {self.harness!r}, which is not one of the "
                f"pinned clients ({pinned}); a score is a model-and-harness pair "
                f"until the harness is one of the pins")
        if not self.timeout_sec:
            found.append(
                "[agent] declares no timeout_sec, so nothing states the clock the "
                "run is entitled to and a driver's flag becomes the only record")
        if self.network_mode != "no-network":
            found.append(
                f"[agent] network_mode is {self.network_mode!r}; the agent phase is "
                f"offline, which is what makes 'the dependency closure is gone' a "
                f"fact about the source rather than a claim")
        return found

    @classmethod
    def load(cls, path: str | Path) -> "AgentPhase":
        """Read ``[agent]`` out of a task descriptor."""
        path = Path(path)
        raw = tomlcompat.load(path)
        block = raw.get("agent")
        if block is None:
            raise ConfigError(f"{path}: no [agent] block; the agent phase's clock "
                              f"and harness are declared there")
        return cls.from_dict(block, str(path))


@dataclass
class Evaluation:
    """``tests/evaluation.toml`` — the per-task orchestration file."""

    task: str
    scoring: ScoringPolicy
    stages: dict[str, StageConfig]
    root: Path
    #: Where stage results are written and read.  Relative paths resolve against
    #: this, so a task never has to hard-code /logs.
    results_dir: str = "/logs/verifier"
    title: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    KNOWN: ClassVar[tuple[str, ...]] = (
        "schema", "task", "scoring", "stages", "results_dir", "title", "metadata",
    )

    @classmethod
    def load(cls, path: str | Path) -> "Evaluation":
        path = Path(path)
        raw = _read_toml(path)
        where = str(path)
        _check_schema(raw, EVALUATION_SCHEMA, where)
        _check_keys(raw, cls.KNOWN, where)
        task = str(_require(raw, "task", where))

        scoring_raw = raw.get("scoring")
        if scoring_raw is not None and not isinstance(scoring_raw, dict):
            raise ConfigError(f"{where}: [scoring] must be a table")
        scoring = ScoringPolicy.from_dict(scoring_raw or {}, where)

        stages_raw = raw.get("stages") or {}
        if not isinstance(stages_raw, dict):
            raise ConfigError(f"{where}: [stages] must be a table")
        stages = {
            name: StageConfig.from_dict(name, sub, where)
            for name, sub in stages_raw.items()
        }
        unknown = sorted(set(stages) - {"audit", "behavioural", "verification"})
        if unknown:
            raise ConfigError(
                f"{where}: unknown stage(s) {', '.join(unknown)}; the ladder is "
                f"audit -> behavioural -> verification"
            )
        for required in ("audit", "behavioural"):
            if required not in stages:
                raise ConfigError(f"{where}: [stages.{required}] is mandatory")

        return cls(
            task=task,
            scoring=scoring,
            stages=stages,
            root=path.parent,
            results_dir=str(raw.get("results_dir", "/logs/verifier")),
            title=str(raw.get("title", "")),
            metadata=raw.get("metadata") or {},
        )

    def stage(self, name: str) -> StageConfig:
        try:
            return self.stages[name]
        except KeyError:
            raise ConfigError(f"{self.task}: no [stages.{name}] in evaluation.toml")

    def result_path(self, stage: str) -> Path:
        """Absolute path of one stage's result file."""
        cfg = self.stage(stage)
        p = Path(cfg.result)
        return p if p.is_absolute() else Path(self.results_dir) / p

    def resolve(self, relative: str | Path) -> Path:
        """A path in evaluation.toml, resolved against the tests/ directory."""
        p = Path(relative)
        return p if p.is_absolute() else (self.root / p)


# --------------------------------------------------------------------------- #
# tests/behavioural/suite.toml
# --------------------------------------------------------------------------- #


@dataclass
class ModuleConfig:
    """One behavioural module: what to run, how long to wait, what it is worth.

    ``weight`` is the module's share of the behavioural stage, normalised across
    the suite — so weights are readable as relative importance and a task author
    does not have to make them sum to exactly one.

    ``dir`` defaults to ``modules/<id>``, ``command`` to that directory's
    ``run.sh``.  A module that fits the convention therefore declares three
    fields, and one that does not can override every one of them.
    """

    id: str
    title: str = ""
    weight: float = 1.0
    command: list[str] = field(default_factory=list)
    dir: str = ""
    timeout_sec: float = 900.0
    #: Extra environment for this module only, on top of the suite's.
    env: dict[str, str] = field(default_factory=dict)
    about: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    #: ``required`` is not a module key.  Stage 2 is all-or-nothing over every
    #: weighted module, so there is no such thing as an optional one: a module that
    #: leaves a scored check unanswered stops the stage whatever its weight, and a
    #: module's weight decides only its share of the rate.  Deliberately absent from
    #: ``KNOWN`` so a suite that declares it fails loudly, with the message in
    #: ``from_dict`` below; silently ignoring it would leave an author believing the
    #: stage draws a distinction it does not draw.
    KNOWN: ClassVar[tuple[str, ...]] = (
        "id", "title", "weight", "command", "dir", "timeout_sec", "env",
        "about", "metadata",
    )

    @classmethod
    def from_dict(cls, raw: dict[str, Any], where: str) -> "ModuleConfig":
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: each [[module]] must be a table")
        mid = str(_require(raw, "id", f"{where} [[module]]"))
        if "/" in mid or mid.startswith("."):
            raise ConfigError(f"{where}: module id {mid!r} must be a bare name")
        if "required" in raw:
            raise ConfigError(
                f"{where} [{mid}]: `required` is not a module key and must be "
                f"removed — the behavioural stage is all-or-nothing over every "
                f"weighted module, so a module's importance is expressed by its "
                f"`weight` alone.  Left in place it would read as a distinction "
                f"between mandatory and optional modules that the stage does not "
                f"make."
            )
        # `wieght = 0.26` used to normalise as an ordinary module of weight 1.0,
        # which is the whole of a suite's arithmetic changed by two letters.
        _check_keys(raw, cls.KNOWN, f"{where} [{mid}]")
        command = raw.get("command", [])
        if isinstance(command, str):
            raise ConfigError(
                f"{where} [{mid}]: command must be a list of arguments, not a "
                f"string — no shell is involved, so quoting would not work"
            )
        if not isinstance(command, list):
            raise ConfigError(f"{where} [{mid}]: command must be a list")
        # `_number` rather than a `float()` in a `try`, for the bool it rejects:
        # `weight = true` converted to 1.0 and normalised as an ordinary module,
        # which is the one wrong value a weight typo is likely to be.
        weight = _number(raw, "weight", 1.0, f"{where} [{mid}]")
        if weight < 0:
            raise ConfigError(f"{where} [{mid}]: weight must be >= 0")
        env = raw.get("env") or {}
        if not isinstance(env, dict):
            raise ConfigError(f"{where} [{mid}]: env must be a table")
        return cls(
            id=mid,
            title=str(raw.get("title", mid)),
            weight=weight,
            command=[str(c) for c in command],
            dir=str(raw.get("dir", "")) or f"modules/{mid}",
            timeout_sec=_number(raw, "timeout_sec", 900.0, f"{where} [{mid}]"),
            env={str(k): str(v) for k, v in env.items()},
            about=str(raw.get("about", "")),
            metadata=raw.get("metadata") or {},
        )


@dataclass
class Suite:
    """``tests/behavioural/suite.toml`` — the module manifest."""

    task: str
    modules: list[ModuleConfig]
    root: Path
    #: Environment handed to every module.  ``$REPO``-style expansion is not
    #: performed: the runner exports the well-known variables itself.
    env: dict[str, str] = field(default_factory=dict)
    lib_dir: str = "lib"
    default_timeout_sec: float = 900.0
    metadata: dict[str, Any] = field(default_factory=dict)

    KNOWN: ClassVar[tuple[str, ...]] = (
        "schema", "task", "module", "env", "lib_dir", "default_timeout_sec",
        "metadata",
    )

    @classmethod
    def load(cls, path: str | Path, *, schema: str = SUITE_SCHEMA) -> "Suite":
        path = Path(path)
        raw = _read_toml(path)
        where = str(path)
        _check_schema(raw, schema, where)
        _check_keys(raw, cls.KNOWN, where)
        task = str(_require(raw, "task", where))
        entries = raw.get("module") or []
        if not isinstance(entries, list) or not entries:
            raise ConfigError(f"{where}: needs at least one [[module]]")
        default_timeout = _number(raw, "default_timeout_sec", 900.0, where)
        modules = []
        for entry in entries:
            mod = ModuleConfig.from_dict(entry, where)
            if "timeout_sec" not in entry:
                mod.timeout_sec = default_timeout
            modules.append(mod)
        ids = [m.id for m in modules]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ConfigError(f"{where}: duplicate module id(s): {', '.join(dupes)}")
        if sum(m.weight for m in modules) <= 0:
            raise ConfigError(f"{where}: module weights sum to zero")
        env = raw.get("env") or {}
        if not isinstance(env, dict):
            raise ConfigError(f"{where}: [env] must be a table")
        return cls(
            task=task,
            modules=modules,
            root=path.parent,
            env={str(k): str(v) for k, v in env.items()},
            lib_dir=str(raw.get("lib_dir", "lib")),
            default_timeout_sec=default_timeout,
            metadata=raw.get("metadata") or {},
        )

    @property
    def weight_total(self) -> float:
        return sum(m.weight for m in self.modules)


# --------------------------------------------------------------------------- #
# tests/verification/probe.toml
# --------------------------------------------------------------------------- #


@dataclass
class Adversary:
    """One attacking model and its budget.

    ``id`` is what the report names, ``model`` is what the driver is asked for.
    Two adversaries may share a ``model`` with different budgets or prompts; the
    id is what makes them distinct rounds.
    """

    id: str
    model: str
    driver: str = "anthropic"
    budget_sec: float = 3600.0
    options: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    #: ``ClassVar`` for the same reason as ``StageConfig.KNOWN``: as a field it
    #: rode along in every adversary's ``repr`` and equality.
    KNOWN: ClassVar[tuple[str, ...]] = ("id", "model", "driver", "budget_sec",
                                        "metadata")

    @classmethod
    def from_dict(cls, raw: dict[str, Any], where: str) -> "Adversary":
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: each [[adversary]] must be a table")
        aid = str(_require(raw, "id", f"{where} [[adversary]]"))
        # A bag, for `driver_options`; near-misses only.
        _check_keys(raw, cls.KNOWN, f"{where} [[adversary]] {aid}", bag=True)
        return cls(
            id=aid,
            model=str(_require(raw, "model", f"{where} [[adversary]] {aid}")),
            driver=str(raw.get("driver", "anthropic")),
            budget_sec=_number(raw, "budget_sec", 3600.0,
                               f"{where} [[adversary]] {aid}"),
            options={k: v for k, v in raw.items() if k not in cls.KNOWN},
            metadata=raw.get("metadata") or {},
        )


@dataclass
class ScopeRules:
    """What a candidate test is allowed to attack.

    Without this the round is unwinnable and therefore uninformative: an
    adversary that may assert anything will always find *something* — a timing
    difference, an ordering of a set, the exact wording of an error message the
    task never promised.  The scope is the task's own public contract, written
    down once, and adjudication rejects a candidate that steps outside it.

    ``allow`` and ``deny`` are prose statements handed to the adjudicator, not
    patterns: the judgement "this asserts on an internal detail" is semantic.
    ``deny_commands`` and ``require_deterministic`` are the mechanical half.
    """

    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    deny_commands: list[str] = field(default_factory=list)
    require_deterministic: bool = True
    #: A candidate has to reproduce this many times, identically, to count.  One
    #: run cannot distinguish a real divergence from a flake, and a flake that
    #: costs the submission ten points is a bug in the benchmark.
    reruns: int = 3
    network_allowed: bool = False
    #: Exit codes from the candidate command that mean the tree could not be tested,
    #: rather than the candidate having something to say about it.  Empty means the
    #: default in ``verification.FAULT_EXIT_CODES`` — sysexits' 64-78, which is the
    #: range the twenty scripts already use for "does not build" and "would not
    #: start".  A task overrides it only if its candidate command answers in a
    #: different vocabulary; the codes below 64 are never in it, because 0 and 1 are
    #: the candidate's own verdict and 2-5 are pytest's own troubles.
    fault_exit_codes: list[int] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    KNOWN: ClassVar[tuple[str, ...]] = (
        "allow", "deny", "deny_commands", "require_deterministic", "reruns",
        "network_allowed", "fault_exit_codes", "metadata",
    )

    @classmethod
    def from_dict(cls, raw: dict[str, Any], where: str = "[scope]") -> "ScopeRules":
        raw = raw or {}
        _check_keys(raw, cls.KNOWN, where)
        # `max(1, ...)` clamps a 0 or a negative, which is the right reading of
        # "reruns = 0" -- one run is the floor the reproduction rule needs -- but
        # it clamped a 2.5 to 2 silently as well, and reruns is what decides
        # whether a candidate counts.  A fraction is a typo, so it is named.
        reruns = _number(raw, "reruns", 3, where)
        if reruns != int(reruns):
            raise ConfigError(f"{where}: reruns must be a whole number, "
                              f"not {reruns!r}")
        return cls(
            allow=[str(x) for x in raw.get("allow") or []],
            deny=[str(x) for x in raw.get("deny") or []],
            deny_commands=[str(x) for x in raw.get("deny_commands") or []],
            require_deterministic=_bool(raw, "require_deterministic", True, where),
            reruns=max(1, int(reruns)),
            # The one field where the quoted-bool slip opened something rather
            # than closing it: `network_allowed = "false"` was True, so a task
            # that had written the network shut ran with it open.
            network_allowed=_bool(raw, "network_allowed", False, where),
            fault_exit_codes=_exit_codes(raw, "fault_exit_codes", where),
            metadata=raw.get("metadata") or {},
        )


@dataclass
class Probe:
    """``tests/verification/probe.toml`` — stage 3's configuration."""

    task: str
    adversaries: list[Adversary]
    scope: ScopeRules
    root: Path
    prompt: str = "prompt.txt"
    #: Command that runs one candidate test against a built repository.  The
    #: adjudicator invokes it twice — against original and against submission —
    #: and compares.  Per task, because "run this test" means cargo, pytest, mvn
    #: or ctest depending on the repository.
    candidate_command: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    KNOWN: ClassVar[tuple[str, ...]] = (
        "schema", "task", "adversary", "scope", "prompt", "candidate_command",
        "metadata",
    )

    @classmethod
    def load(cls, path: str | Path) -> "Probe":
        path = Path(path)
        raw = _read_toml(path)
        where = str(path)
        _check_schema(raw, PROBE_SCHEMA, where)
        _check_keys(raw, cls.KNOWN, where)
        entries = raw.get("adversary") or []
        if not isinstance(entries, list):
            raise ConfigError(f"{where}: [[adversary]] must be a list of tables")
        adversaries = [Adversary.from_dict(e, where) for e in entries]
        ids = [a.id for a in adversaries]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ConfigError(f"{where}: duplicate adversary id(s): {', '.join(dupes)}")
        command = raw.get("candidate_command") or []
        if isinstance(command, str):
            raise ConfigError(f"{where}: candidate_command must be a list")
        return cls(
            task=str(_require(raw, "task", where)),
            adversaries=adversaries,
            scope=ScopeRules.from_dict(raw.get("scope") or {}, f"{where} [scope]"),
            root=path.parent,
            prompt=str(raw.get("prompt", "prompt.txt")),
            candidate_command=[str(c) for c in command],
            metadata=raw.get("metadata") or {},
        )
