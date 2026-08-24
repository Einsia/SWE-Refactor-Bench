"""Command line for the three stages, the scorer, and the config validator.

    swerefactor validate     --task-dir tasks/lang01-...
    swerefactor audit    --task-dir ... --repo /workspace/repo
    swerefactor behavioural   --task-dir ... --repo /workspace/repo
    swerefactor verification  --task-dir ... --repo /workspace/repo
    swerefactor score        --task-dir ...
    swerefactor report       --verdict /logs/verifier/score.json

Exit codes describe whether the *stage ran*, never what it decided.  A failed
audit gate is a successful run of the gate, and a pipeline that could not tell
those apart without parsing JSON would eventually conflate an outage with a
cheating submission:

    0   the stage ran and wrote its result
    1   the stage could not run -- missing tree, unreachable model, dead gateway
    2   the task's own configuration is wrong (an authoring bug)

A broken suite or evaluation file is 2, not 1: those are the task's own files, and
a pipeline that reads 1 will retry an authoring bug until its budget runs out.

The verdict is in the JSON in every case, and the JSON exists in every case --
including when the unreadable file is the one that says where the JSON goes.  See
``_resolve_for``.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
import tarfile
import traceback
from pathlib import Path
from typing import Any

from . import (audit, behavioural, config, ladder, models, report, result,
               scan, scoring, telemetry, tomlcompat, verification)

DEFAULT_RESULTS = "/logs/verifier"
DEFAULT_ORIGINAL = "/opt/original"
DEFAULT_REPO = "/workspace/repo"

CONFIG_ERROR = 2
RUN_ERROR = 1


def _log(message: str) -> None:
    print(f"[swerefactor] {message}", flush=True)


def _die(message: str, code: int = RUN_ERROR) -> int:
    print(f"[swerefactor] error: {message}", file=sys.stderr, flush=True)
    return code


def _driver_for(spec: dict[str, Any], args: argparse.Namespace,
                default_model: str) -> models.Driver:
    """Build a driver from stage options, with command-line overrides.

    ``--driver scripted --script turns.json`` is how a new task's prompt gets
    exercised without spending a budget on it.
    """
    name = args.driver or str(spec.get("driver") or "anthropic")
    model = args.model or str(spec.get("model") or default_model)
    options = dict(spec.get("driver_options") or {})
    if args.script:
        options["script"] = args.script
    return models.build(name, model, options=options)


def _resolve(args: argparse.Namespace) -> tuple[config.Evaluation, Path]:
    task_dir = Path(args.task_dir).resolve()
    path = task_dir / "tests" / "evaluation.toml"
    if not path.exists():
        raise config.ConfigError(f"no evaluation.toml at {path}")
    evaluation = config.Evaluation.load(path)
    if args.results:
        evaluation.results_dir = args.results
    return evaluation, task_dir


def _resolve_for(args: argparse.Namespace,
                 stage: result.Stage) -> tuple[config.Evaluation, Path]:
    """``_resolve``, but a broken ``evaluation.toml`` still leaves a result file.

    Every other failure path in the three stage commands writes one before it
    dies, because the rule in ``docs/SCHEMA.md`` is that the verdict lives in the
    JSON and the exit code says only whether the stage ran.  This path could not
    keep that rule: the result *path* is read out of ``evaluation.toml``, so a
    file that will not parse takes the answer and the place to put the answer
    with it, and the stage exited having written nothing at all.  A harness that
    reads results rather than exit codes -- which is the one this contract asks
    for -- then sees a missing file, which is stage 3's "the ladder stopped here"
    and not "this task is misconfigured".

    The fallback is the default the schema documents and all twenty tasks
    declare: ``<results_dir>/<stage>.json``, with ``--results`` honoured because
    it is on the command line rather than in the unreadable file.  A task that
    overrode ``result`` and then broke its own config gets the file in the
    default place instead of its chosen one; that is a worse guess than reading
    the file, and a better one than silence.

    The task id is the directory name, which is where it comes from for all
    twenty.  It is *not* authoritative -- ``task`` is a field in the file that
    just failed to load -- so the note says where the name came from rather than
    presenting it as read.
    """
    try:
        return _resolve(args)
    except config.ConfigError as exc:
        out = Path(args.results or DEFAULT_RESULTS) / f"{stage}.json"
        res = result.StageResult.failed(
            stage, Path(args.task_dir).resolve().name, str(exc))
        res.note(f"task id inferred from the directory name; "
                 f"{Path(args.task_dir).resolve() / 'tests' / 'evaluation.toml'} "
                 f"could not be read")
        res.metadata["config_error"] = True
        try:
            res.write(out)
        except OSError as write_exc:
            # The config error is the one worth reporting.  Losing the result
            # file too is worse, but it is not a different diagnosis, and
            # raising from here would replace a message naming the broken key
            # with one naming a directory.
            print(f"[swerefactor] warning: could not write {out}: {write_exc}",
                  file=sys.stderr, flush=True)
        else:
            _log(f"wrote {out}")
        raise


def _log_dir(evaluation: config.Evaluation, stage: str,
             args: argparse.Namespace) -> Path:
    """The stage's log directory, with any previous run's transcripts removed.

    Transcripts are named by sample index -- `review-1.jsonl` and friends -- so a
    second run into the same directory overwrites them one at a time, as each
    sample finishes.  Between the two, the directory holds a mixture: the live
    run's early samples beside a dead run's later ones, with nothing in the names
    to say which is which.

    That was observed, and on the stage where it matters most.  A run whose three
    samples all failed on a misconfigured gateway left `review-3.jsonl` carrying
    `$OPENAI_EXTRA_HEADERS is not JSON`; the next run started 39 seconds later,
    and for five minutes that file sat beside the live transcripts looking like
    part of the run in progress.  The stage's own rule is that a reviewer's every
    citation gets re-opened before it can fail a gate, and the same standard has
    to hold for the evidence about the review: reading a stale transcript as this
    run's is exactly the mistake the stage exists to prevent.

    Only transcripts are removed, and only from this stage's own directory.  The
    result files a previous stage wrote are somebody else's evidence.
    """
    base = Path(args.log_dir) if args.log_dir else Path(evaluation.results_dir)
    path = base / stage
    path.mkdir(parents=True, exist_ok=True)
    for stale in sorted(path.glob("review-*.jsonl")) + sorted(path.glob("round-*.jsonl")):
        try:
            stale.unlink()
        except OSError:
            # Reported rather than fatal: a directory that cannot be cleaned is
            # worth knowing about, and refusing to grade over it would turn a
            # permissions problem into a zero.
            print(f"warning: could not remove stale transcript {stale}",
                  file=sys.stderr)
    return path


def _stage_work(args: argparse.Namespace, name: str) -> Path:
    """Scratch for a stage that has no ``--work`` of its own."""
    base = Path(getattr(args, "work", "") or f"/tmp/swerefactor-{name}")
    base.mkdir(parents=True, exist_ok=True)
    return base


def cmd_validate(args: argparse.Namespace) -> int:
    """Parse everything a task declares and report every problem at once.

    Run while authoring a task.  It touches no model and no submission, so it is
    fast enough to run on every edit, and it is the difference between finding a
    missing gate id now and finding it after six rounds have been paid for.
    """
    task_dir = Path(args.task_dir).resolve()
    problems: list[str] = []
    notes: list[str] = []

    try:
        evaluation = config.Evaluation.load(task_dir / "tests" / "evaluation.toml")
    except config.ConfigError as exc:
        return _die(str(exc), CONFIG_ERROR)
    notes.append(f"task {evaluation.task}")

    audit = evaluation.stage("audit")
    if not audit.gates:
        problems.append("[stages.audit] declares no gates, so the hard gate "
                        "cannot fail anything")
    prompt = evaluation.resolve(
        audit.options.get("prompt") or "audit/prompt.txt")
    if not prompt.exists():
        problems.append(f"audit prompt missing: {prompt}")
    else:
        text = prompt.read_text(encoding="utf-8")
        for gate in audit.gates:
            if gate.id not in text and "{{gates}}" not in text:
                problems.append(
                    f"gate {gate.id!r} is declared but never mentioned in "
                    f"prompt.txt, and the prompt has no {{{{gates}}}} placeholder "
                    f"-- the review will not be asked about it"
                )
        notes.append(f"{len(audit.gates)} gate(s), "
                     f"{sum(1 for g in audit.gates if g.required)} required")

    scan_rel = audit.options.get("scan")
    if scan_rel:
        scan_path = evaluation.resolve(scan_rel)
        if not scan_path.exists():
            problems.append(f"scan suite missing: {scan_path}")
        else:
            try:
                scan_suite = config.Suite.load(scan_path, schema=config.SCAN_SCHEMA)
            except config.ConfigError as exc:
                problems.append(str(exc))
            else:
                if scan_suite.task != evaluation.task:
                    problems.append(
                        f"{scan_path.name} says task = {scan_suite.task!r} but "
                        f"evaluation.toml says {evaluation.task!r}")
                for module in scan_suite.modules:
                    mdir = scan_path.parent / module.dir
                    if not mdir.exists():
                        problems.append(
                            f"scan module {module.id!r}: no directory at {mdir}")
                    elif not module.command and not any(
                            (mdir / n).exists() for n in ("run.sh", "run.py")):
                        problems.append(
                            f"scan module {module.id!r}: no command declared and "
                            f"neither run.sh nor run.py exists in {mdir}")
                if prompt.exists() and "{{findings}}" not in prompt.read_text(
                        encoding="utf-8"):
                    problems.append(
                        "a scan is declared but prompt.txt has no {{findings}} "
                        "placeholder, so its observations would be computed and "
                        "then thrown away")
                notes.append(f"{len(scan_suite.modules)} scan module(s) "
                             f"(advisory: they inform the gates, none of them "
                             f"gates)")

    suite_path = evaluation.resolve(
        evaluation.stage("behavioural").options.get("suite") or "behavioural/suite.toml")
    if not suite_path.exists():
        problems.append(f"behavioural suite missing: {suite_path}")
    else:
        try:
            suite = config.Suite.load(suite_path)
        except config.ConfigError as exc:
            problems.append(str(exc))
        else:
            if suite.task != evaluation.task:
                problems.append(
                    f"suite.toml says task = {suite.task!r} but evaluation.toml "
                    f"says {evaluation.task!r}")
            for module in suite.modules:
                mdir = suite_path.parent / module.dir
                if not mdir.exists():
                    problems.append(f"module {module.id!r}: no directory at {mdir}")
                elif not module.command and not any(
                        (mdir / n).exists() for n in ("run.sh", "run.py")):
                    problems.append(
                        f"module {module.id!r}: no command declared and neither "
                        f"run.sh nor run.py exists in {mdir}")
            notes.append(f"{len(suite.modules)} behavioural module(s), weights "
                         f"summing to {suite.weight_total:g}")

    if "verification" in evaluation.stages:
        probe_path = evaluation.resolve(
            evaluation.stage("verification").options.get("probe")
            or "verification/probe.toml")
        if not probe_path.exists():
            problems.append(f"verification probe missing: {probe_path}")
        else:
            try:
                probe = config.Probe.load(probe_path)
            except config.ConfigError as exc:
                problems.append(str(exc))
            else:
                want = evaluation.scoring.verification_models
                if len(probe.adversaries) != want:
                    problems.append(
                        f"probe.toml declares {len(probe.adversaries)} adversaries "
                        f"but [scoring] pays for {want}; every missing one is "
                        f"{evaluation.scoring.points_per_survived_model:g} points "
                        f"nobody can earn")
                if not probe.candidate_command:
                    problems.append("probe.toml: candidate_command is empty, so no "
                                    "candidate test can be run")
                if not probe.scope.allow and not probe.scope.deny:
                    problems.append("probe.toml [scope]: neither allow nor deny is "
                                    "stated, so every divergence counts and no "
                                    "submission can survive the round")
                pp = probe_path.parent / probe.prompt
                if not pp.exists():
                    problems.append(f"verification prompt missing: {pp}")
                notes.append(f"{len(probe.adversaries)} adversary/ies, "
                             f"{probe.scope.reruns} rerun(s) required")

    problems.extend(_stage_images(evaluation, notes))
    problems.extend(_model_dialects(evaluation, notes))

    env_dir = task_dir / "environment"
    for name in ("Dockerfile", "original.tar.gz", "original.sha256"):
        if not (env_dir / name).exists():
            problems.append(f"environment/{name} is missing")
    for name in ("task.toml", "instruction.md", "score.py"):
        if not (task_dir / name).exists():
            problems.append(f"{name} is missing")

    problems.extend(_agent_phase(task_dir, notes))

    problems.extend(_shared_input_drift(task_dir, env_dir))
    problems.extend(_recorded_digest_drift(task_dir, env_dir, notes))
    problems.extend(_state_a_excerpt_drift(task_dir, env_dir))
    problems.extend(_collected_fixture_loss(task_dir, env_dir, notes))
    problems.extend(_collected_output_loss(task_dir, evaluation, notes))
    problems.extend(_ladder_budget(task_dir, evaluation, notes))
    problems.extend(_instruction_arithmetic(task_dir, evaluation, notes))
    problems.extend(_stage_layout(task_dir, evaluation))

    problems.extend(_copy_sources(env_dir, "environment"))
    for name, stage in sorted(evaluation.stages.items()):
        context = str(stage.options.get("context") or "")
        if stage.enabled and context:
            cdir = evaluation.resolve(context)
            if cdir.is_dir():
                problems.extend(_copy_sources(cdir, f"[stages.{name}]"))

    for note in notes:
        _log(note)
    if problems:
        print(f"\n{len(problems)} problem(s) in {task_dir.name}:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return CONFIG_ERROR
    _log(f"{task_dir.name}: configuration is consistent")
    return 0


def _agent_phase(task_dir: Path, notes: list[str]) -> list[str]:
    """The agent phase declares its harness and its clock, and both are read here.

    Checked at validate time because that is the last moment before a run costs
    money.  A task that declares a harness nobody runs, or a budget no driver
    honours, produces a number that looks like every other number: the corpus's
    lang04 declared 30 hours, was launched with 3, stopped mid-port and scored 0.0.
    """
    task_toml = task_dir / "task.toml"
    if not task_toml.is_file():
        return []
    try:
        agent = config.AgentPhase.load(task_toml)
    except config.ConfigError as exc:
        return [str(exc)]
    except (OSError, ValueError) as exc:
        return [f"task.toml could not be parsed to check [agent]: {exc}"]
    if not agent.problems:
        notes.append(f"agent phase: {agent.harness}, "
                     f"{agent.timeout_sec / 3600:.1f}h, {agent.network_mode}")
    return agent.problems


def _model_dialects(evaluation: config.Evaluation, notes: list[str]) -> list[str]:
    """Every declared model must be paired with the driver its family speaks.

    ``driver`` defaults to ``anthropic``, so a task that names an OpenAI model and
    forgets the line gets a request posted to ``/v1/messages`` with Anthropic's
    tool schema.  That is not a config error anything catches: it is a run that
    starts, spends its timeout failing, and reports a harness fault per sample.

    The reverse direction is the one that cost a stage.  A ``claude-*`` model with
    no ``driver`` line is correctly served, so nothing is wrong -- but the line's
    absence is also the only difference between the task that hit the tool-name
    collision and the nineteen that did not, and reading nineteen files to notice
    which one is on the other dialect is not a review anyone performs.  So it is
    reported as a note when right and a problem when wrong.
    """
    problems: list[str] = []
    declared: list[tuple[str, str, str]] = []   # where, model, driver

    for name, stage in sorted(evaluation.stages.items()):
        if not stage.enabled:
            continue
        model = str(stage.options.get("model") or "")
        if model:
            declared.append((f"[stages.{name}]", model,
                             str(stage.options.get("driver") or "")))
        # The adjudicator is a separate model call with its own driver, built by
        # _driver_for(judge_spec, ...) -- so it defaults independently of the
        # stage's own line and has to be checked on its own.
        judge = stage.options.get("adjudicator")
        if isinstance(judge, dict) and judge.get("model"):
            declared.append((f"[stages.{name}.adjudicator]",
                             str(judge["model"]),
                             str(judge.get("driver") or "")))

    if "verification" in evaluation.stages:
        probe_path = evaluation.resolve(
            evaluation.stage("verification").options.get("probe")
            or "verification/probe.toml")
        if probe_path.exists():
            try:
                probe = config.Probe.load(probe_path)
            except config.ConfigError:
                probe = None            # already reported by the caller
            if probe is not None:
                for adv in probe.adversaries:
                    declared.append((f"probe.toml [[adversary]] {adv.id}",
                                     adv.model, adv.driver))

    dialects: set[str] = set()
    for where, model, driver in declared:
        wanted = models.dialect_for(model)
        # An explicit driver we do not recognise is build()'s complaint, not ours.
        if driver and driver not in models.DRIVERS:
            problems.append(f"{where}: unknown driver {driver!r}; known drivers "
                            f"are {', '.join(sorted(models.DRIVERS))}")
            continue
        if driver in ("scripted", "command"):
            continue                    # a local replacement speaks no dialect
        effective = driver or "anthropic"
        dialects.add(effective)
        if wanted is None:
            notes.append(f"{where}: model {model!r} on the {effective} driver "
                         f"(the name does not imply a dialect, so it is taken "
                         f"as declared)")
            continue
        if effective != wanted:
            problems.append(
                f"{where}: model {model!r} speaks the {wanted} dialect but "
                + (f"driver = {driver!r} is declared"
                   if driver else
                   f"no driver is declared, so it defaults to anthropic")
                + f" -- add driver = \"{wanted}\""
            )
        elif not driver:
            problems.append(
                f"{where}: model {model!r} is correctly served by the default "
                f"anthropic driver, but the line is missing -- state "
                f"driver = \"{wanted}\" so which dialect this stage runs on is "
                f"readable without knowing the default"
            )

    if dialects:
        notes.append(f"model dialect(s) in use: {', '.join(sorted(dialects))}")
    return problems


def _stage_images(evaluation: config.Evaluation, notes: list[str]) -> list[str]:
    """Check that each stage's declared build context is really there.

    A stage declares ``image`` and ``context`` so a runner can find out which
    Dockerfile builds it without a naming convention to remember.  Left unchecked
    that is a path in a comment: it goes stale the first time a directory is
    renamed, and the failure surfaces as a build error in CI rather than as a
    problem with the task.  Declaring a context and not shipping one is the same
    class of mistake as declaring a gate no prompt mentions.
    """
    problems: list[str] = []
    seen: dict[str, str] = {}
    for name, stage in sorted(evaluation.stages.items()):
        if not stage.enabled:
            continue
        image = str(stage.options.get("image") or "")
        context = str(stage.options.get("context") or "")
        if not image and not context:
            continue  # a stage may legitimately run in the verifier's own image
        if not context:
            problems.append(f"[stages.{name}] declares image = {image!r} but no "
                            f"context, so nothing says what builds it")
            continue
        cdir = evaluation.resolve(context)
        if not cdir.is_dir():
            problems.append(f"[stages.{name}] context = {context!r}: no directory "
                            f"at {cdir}")
        elif not (cdir / "Dockerfile").exists():
            problems.append(f"[stages.{name}] context = {context!r}: no Dockerfile "
                            f"in {cdir}")
        if image:
            # Two stages sharing a tag would silently overwrite each other, and
            # which one you then ran would depend on build order.
            if image in seen:
                problems.append(
                    f"[stages.{name}] image = {image!r} is already used by "
                    f"[stages.{seen[image]}]; one tag cannot be two images")
            else:
                seen[image] = name
    if seen:
        notes.append(f"{len(seen)} stage image(s): "
                     + ", ".join(f"{s}={i}" for i, s in sorted(seen.items())))
    return problems


def _shared_input_drift(task_dir: Path, env_dir: Path) -> list[str]:
    """Every stage's copy of a shared input must equal the environment's copy.

    A Docker build context cannot reach outside itself, so an input the agent's
    environment and a stage image both need exists twice on disk.  That is
    tolerable; the same input existing twice with *different contents* is not.
    Stage 3 asserts "the original passes this test", and it can only mean that if
    the original it builds is the one the agent developed against.  Stage 2 says
    "installs from the same wheel set the agent had".  Both claims are about a
    file, and both are false the moment one copy is regenerated alone.

    So: same basename under ``environment/`` and anywhere under ``tests/`` means
    same bytes.  No manifest to maintain -- a new shared input is covered the day
    it is added, which is why this matches on name rather than on a list.  It was
    a list once, spelled ``requirements*``, and it went stale immediately: the
    Maven warm-up cache, the State A tarball and the source contract are all
    duplicated for exactly the same reason and none of them was being checked.

    Directories are compared entry by entry, since a warm-up cache is a tree.
    """
    problems: list[str] = []
    tests_dir = task_dir / "tests"
    if not env_dir.is_dir() or not tests_dir.is_dir():
        return problems

    # The environment's copy is canonical: it is the one the agent actually got.
    # A Dockerfile is excluded because it *describes* a context rather than being
    # an input to one -- four images that shared a Dockerfile would be one image.
    canonical = {p.name: p for p in env_dir.iterdir()
                 if not p.name.startswith(".") and p.name != "Dockerfile"}
    if not canonical:
        return problems

    def _differs(copy: Path, source: Path) -> str:
        """Empty string if the two paths hold the same bytes, else why not."""
        if source.is_dir() != copy.is_dir():
            return "one is a directory and the other is a file"
        if source.is_file():
            return "" if copy.read_bytes() == source.read_bytes() else "contents differ"
        rel_src = {p.relative_to(source) for p in source.rglob("*") if p.is_file()}
        rel_cpy = {p.relative_to(copy) for p in copy.rglob("*") if p.is_file()}
        if rel_src != rel_cpy:
            only = sorted(str(p) for p in rel_src ^ rel_cpy)[:3]
            return f"different file lists (e.g. {', '.join(only)})"
        for rel in sorted(rel_src):
            if (copy / rel).read_bytes() != (source / rel).read_bytes():
                return f"contents differ at {rel}"
        return ""

    # Only the top level of each stage context, plus its data/ tree: a stage's
    # lib/ may legitimately hold a module whose name collides with something in
    # environment/, and that is not a copy of it.
    seen: set[Path] = set()
    for stage_dir in sorted(p for p in tests_dir.iterdir() if p.is_dir()):
        candidates = [p for p in stage_dir.iterdir() if not p.name.startswith(".")]
        data_dir = stage_dir / "data"
        if data_dir.is_dir():
            candidates += [p for p in data_dir.iterdir() if not p.name.startswith(".")]
        for copy in sorted(candidates):
            source = canonical.get(copy.name)
            if source is None or copy in seen:
                continue
            seen.add(copy)
            why = _differs(copy, source)
            if why:
                problems.append(
                    f"{copy.relative_to(task_dir)} differs from "
                    f"{source.relative_to(task_dir)} ({why}); a stage grading "
                    f"against different inputs from the ones the agent was given "
                    f"is not grading the task (copy the environment's over it)"
                )
    return problems


def _tarball_fact(tarball: Path, kind: str) -> int | None:
    """The tarball's size in bytes, or the number of regular files in it."""
    if kind == "size":
        return tarball.stat().st_size
    try:
        with tarfile.open(tarball) as tf:
            return sum(1 for mem in tf.getmembers() if mem.isfile())
    except (OSError, tarfile.TarError):
        return None


def _recorded_digest_drift(task_dir: Path, env_dir: Path,
                           notes: list[str]) -> list[str]:
    """A digest written down for a committed file must be that file's digest.

    ``environment/original.sha256`` was checked for existence and never read, and
    existence is the one thing about it that cannot be wrong: the file is written
    once and the tarball is re-pinned later.  ``tools/build_repo_snapshot.py``
    prints the new digest and writes none of the files that record it, so every
    copy is updated by hand, and a re-pin that updates some of them is the whole
    failure.  There are 2 to 13 such copies per task, 119 in the benchmark.

    Which is worth catching here rather than at build time, even though most of
    the builds do check.  Seventeen environments verify the tarball -- eleven by
    running ``sha256sum -c original.sha256``, five against an ``ARG``, pf03 by
    hashing into a shell variable -- so for those a stale digest fails as a build
    error, hours after the authoring mistake, in a log that says a tarball is
    corrupt when the tarball is fine.  lang01, lang03 and lang06 inline the digest
    as a shell literal with no ``ARG`` to declare it, and there ``original.sha256``
    is a file no build reads: documentation that looks like a check.

    Convention rather than a manifest, so a new guarded input is covered the day
    it is added.  Any digest paired with a path is checked against the file that
    path came from, resolved through the Dockerfile's own ``COPY`` map -- lang04
    guards ``/opt/swerefactor/repo.tar.gz``, which is ``original.tar.gz`` renamed on
    the way in.  Tool downloads skip themselves: no ``COPY`` names them, because
    they are not in the tree to name.  The size and file-count ``ARG``s beside
    lang04's and lang05's digests are checked the same way, having been recorded
    for the same reason and drifting for the same reason.

    All four Dockerfiles are read, not just the environment's.  Each stage keeps
    its own ``data/original.tar.gz`` and its own digest beside it; the copies are
    held byte-identical by ``_shared_input_drift``, which makes a stage digest that
    disagrees with the environment's a statement about the same bytes.
    """
    problems: list[str] = []
    tarball = env_dir / "original.tar.gz"
    if not tarball.is_file():
        return problems

    actual = hashlib.sha256(tarball.read_bytes()).hexdigest()

    checked = 0
    recorded = env_dir / "original.sha256"
    if recorded.is_file():
        # `sha256sum` format: digest, two spaces, name.  The name is checked too,
        # because a digest recorded against the wrong filename is a digest that
        # `sha256sum -c` would not have verified either.
        checked += 1
        line = recorded.read_text().split("\n", 1)[0].split()
        if len(line) < 2 or line[0] != actual:
            problems.append(
                f"environment/original.sha256 records {line[0] if line else '(empty)'} "
                f"for original.tar.gz, which is {actual}")
        elif line[1].lstrip("*") != "original.tar.gz":
            problems.append(
                f"environment/original.sha256 records a digest for {line[1]!r} "
                f"rather than for original.tar.gz")

    scans = [("environment/Dockerfile", env_dir / "Dockerfile", env_dir)]
    for stage in ("audit", "behavioural", "verification"):
        sdir = task_dir / "tests" / stage
        if (sdir / "Dockerfile").is_file():
            scans.append((f"tests/{stage}/Dockerfile", sdir / "Dockerfile", sdir))

    for label, dockerfile, context in scans:
        text = dockerfile.read_text()
        args = {m.group(1): m.group(2)
                for m in re.finditer(r"^ARG\s+(\w+)=(\S+)", text, re.M)}

        # Image path -> the build-context file it was copied from.  This is the
        # join, not the basename: lang04 guards `/opt/swerefactor/repo.tar.gz`,
        # which is `original.tar.gz` renamed on the way in, and matching on
        # `repo.tar.gz` finds nothing in the tree and skips the one task most able
        # to drift.  Resolving through COPY also makes the tool downloads skip
        # themselves -- no COPY names them, because they are not in the tree to
        # name, and their digests are upstream's facts rather than ours.
        # `COPY a b c dest/` is legal and pf02 uses it, so the destination is the
        # last token and every token before it is a source.  Reading the second
        # token as the destination maps a real path to the wrong file, which is
        # the failure mode this whole function exists to catch.
        copied: dict[str, Path] = {}
        for m in re.finditer(r"^COPY\s+(.+?)\s*$", text, re.M):
            tokens = [t for t in m.group(1).split() if not t.startswith("--")]
            if len(tokens) < 2:
                continue
            dst, srcs = tokens[-1], tokens[:-1]
            into_dir = len(srcs) > 1 or dst.endswith("/") or dst in (".", "..")
            for src in srcs:
                local = context / src
                if not local.is_file():
                    continue
                if into_dir:
                    copied[dst.rstrip("/") + "/" + Path(src).name] = local
                else:
                    copied[dst] = local

        def resolve(path: str, copied: dict[str, Path] = copied,
                    context: Path = context) -> Path | None:
            """The committed file a guarded image path refers to, if any."""
            path = path.strip("'\"")
            for key in (path, "./" + path.lstrip("./"), path.lstrip("./")):
                if key in copied:
                    return copied[key]
            local = context / Path(path).name
            return local if local.is_file() else None

        # A digest has to be paired with the path `sha256sum` reads, not with any
        # path its RUN block happens to mention -- pf01's zlib block invokes a
        # copied script, so a block-wide match checks the download's digest
        # against build-wasi-zlib.sh and reports a file that is fine as corrupt.
        # Three spellings say it, and the third is the one that most needs saying:
        # `${ARG}  <path> | sha256sum -c`, pf03 hashing into a shell variable and
        # comparing against an `ARG`, and lang01/lang03/lang06 writing the digest
        # as a bare literal that no `ARG` declares and no reader can grep for.
        for block in re.split(r"^(?=(?:RUN|COPY|FROM|ARG|ENV)\s)", text,
                              flags=re.M):
            if not block.startswith("RUN") or "sha256sum" not in block:
                continue
            # `[\s\\]*` and not `\s*`: eleven of these span two lines, and the
            # gap between the path and the pipe is `' \` then a newline, which a
            # whitespace class alone does not cross.
            pairs: list[tuple[str, str, str]] = []
            for m in re.finditer(
                    r"['\"]?\$\{?(\w*SHA256\w*)\}?\s+(\S+?)['\"]?[\s\\]*\|[\s\\]*"
                    r"sha256sum\s+-c", block):
                pairs.append((m.group(1), args.get(m.group(1), ""), m.group(2)))
            for m in re.finditer(
                    r"['\"]?([0-9a-f]{64})\s+(\S+?)['\"]?[\s\\]*\|[\s\\]*"
                    r"sha256sum\s+-c", block):
                pairs.append(("(literal)", m.group(1), m.group(2)))
            if not pairs:
                # Split form.  Only paired when the block hashes one file and
                # names one digest, because then there is nothing else either
                # could mean.
                hashed = re.findall(r"sha256sum\s+(?!-)(\S+)", block)
                names = {n for n in re.findall(r"\$\{?(\w*SHA256\w*)\}?", block)
                         if n in args}
                if len(set(hashed)) == 1 and len(names) == 1:
                    n = names.pop()
                    pairs = [(n, args[n], hashed[0])]
            for name, want, path in pairs:
                local = resolve(path)
                if len(want) != 64 or local is None:
                    continue
                checked += 1
                got = hashlib.sha256(local.read_bytes()).hexdigest()
                if want != got:
                    problems.append(
                        f"{label} records {want[:12]}... for {name} against "
                        f"{local.name}, which hashes to {got[:12]}...; the build "
                        f"would fail on a file that is not corrupt")

        # Sizes and file counts, recorded for the same reason and drifting for the
        # same reason.  Both spellings again: an `ARG` lang04 and lang05 declare,
        # and a literal the other three write into the `test` that reads it.
        for name, kind in (("REPO_SIZE", "size"), ("REPO_FILES", "files")):
            declared = args.get(name)
            if declared is None or not declared.isdigit():
                continue
            got = _tarball_fact(tarball, kind)
            if got is None:
                continue
            checked += 1
            if int(declared) != got:
                problems.append(f"{label} ARG {name}={declared} but "
                                f"original.tar.gz has {kind} {got}")

        for m in re.finditer(r"stat\s+-c\s+'%s'\s+(\S+)\)\"\s*=\s*\"?(\d+)", text):
            local = resolve(m.group(1))
            if local is None:
                continue
            checked += 1
            if local.stat().st_size != int(m.group(2)):
                problems.append(
                    f"{label} expects {local.name} to be {m.group(2)} bytes, but "
                    f"the committed file is {local.stat().st_size}")

    if checked:
        notes.append(f"{checked} recorded digest/size fact(s) match the "
                     f"committed file(s)")
    return problems


def _state_a_excerpt_drift(task_dir: Path, env_dir: Path) -> list[str]:
    """A ``data/statea-*`` file must equal that path inside ``original.tar.gz``.

    ``_shared_input_drift`` above matches on basename, so it covers a whole file
    duplicated beside the environment's copy.  It cannot see a stage that ships
    *part* of State A -- one file lifted out of the tarball, renamed to say where
    it came from.  fw03 does exactly that with State A's ``package.json`` and
    ``yarn.lock``, because its stage-2 image mints a yarn offline mirror from them
    so that a submission still carrying State A's dependency closure installs and
    gets graded on its behaviour instead of dying on a cache miss.

    Unchecked, that is a copy with no owner.  Re-record State A and the mirror is
    minted from the lockfile of a repository that no longer exists: every
    assertion in the image still passes -- the mirror does cover the closure it was
    built from -- and the breakage surfaces only as a submission failing to
    install, with a grader-side cause and no sign of one.  That is the failure
    mode this whole file exists to make impossible.

    Convention rather than a manifest, for the same reason as above: name a file
    ``statea-<path>`` in a stage's ``data/`` and it is checked from that day on.
    A ``/`` in the original path is written as ``-``, so
    ``statea-src-main.js`` means ``src/main.js``.
    """
    problems: list[str] = []
    tarball = env_dir / "original.tar.gz"
    tests_dir = task_dir / "tests"
    if not tarball.is_file() or not tests_dir.is_dir():
        return problems

    excerpts = sorted(p for p in tests_dir.glob("*/data/statea-*") if p.is_file())
    excerpts = [p for p in excerpts if not p.name.endswith((".sha256", ".sha512"))]
    if not excerpts:
        return problems

    try:
        with tarfile.open(tarball) as tf:
            inside = {
                # The tarball holds one top-level directory (the repository), and
                # the excerpt is named for the path within it.
                m.name.split("/", 1)[1]: tf.extractfile(m).read()
                for m in tf.getmembers() if m.isfile() and "/" in m.name
            }
    except (OSError, tarfile.TarError, KeyError) as exc:
        return [f"environment/original.tar.gz could not be read to check "
                f"{len(excerpts)} statea-* excerpt(s): {exc}"]

    for copy in excerpts:
        want = copy.name[len("statea-"):]
        original = inside.get(want)
        if original is None:
            # A dash may stand for a path separator; try that reading too.
            for candidate, blob in inside.items():
                if candidate.replace("/", "-") == want:
                    original = blob
                    break
        rel = copy.relative_to(task_dir)
        if original is None:
            problems.append(
                f"{rel} claims to be State A's {want}, but original.tar.gz has no "
                f"such file; either the excerpt is misnamed or it is a copy of "
                f"something that no longer exists")
        elif copy.read_bytes() != original:
            problems.append(
                f"{rel} differs from {want} inside environment/original.tar.gz; "
                f"a stage image built from this excerpt is built from a State A "
                f"the agent was never given (re-extract it from the tarball)")
    return problems


# What a stage directory is allowed to contain.  The point is not tidiness: a
# reviewer opening any of the twelve should be able to tell code from input from
# manifest without reading a Dockerfile, and "is this file executed or only read?"
# is the question an anti-cheat review turns on.
_STAGE_DIRS = frozenset({"lib", "modules", "data"})
_STAGE_MANIFESTS = frozenset({"suite.toml", "probe.toml", "scan.toml"})


def _copy_sources(context: Path, label: str) -> list[str]:
    """Every path a Dockerfile COPYs from its context must be in that context.

    ``lang04`` shipped for months with ``COPY repo.tar.gz`` against a context
    holding ``original.tar.gz``: the environment image could not build at all, and
    validate called the task consistent because it checked that the Dockerfile
    existed and never read it.  A build-context COPY is the one Dockerfile
    instruction whose operand is a path on *this* disk, so it is the one this can
    check without a daemon.

    ``--from=`` is skipped deliberately: those resolve inside another stage or
    image, which is not something a file listing can answer.
    """
    problems: list[str] = []
    dockerfile = context / "Dockerfile"
    if not dockerfile.is_file():
        return problems
    try:
        text = dockerfile.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{label}: cannot read Dockerfile: {exc}"]

    # Join continuations so a COPY split over several lines reads as one, keeping
    # each logical line's *first* physical line number: a problem report whose
    # line number is off by the number of preceding continuations sends the
    # reader to the wrong instruction, which is worse than no number.
    logical: list[tuple[int, str]] = []
    pending, start = "", 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not pending:
            start = lineno
        if raw.rstrip().endswith("\\"):
            pending += raw.rstrip()[:-1] + " "
            continue
        logical.append((start, pending + raw))
        pending = ""
    if pending:
        logical.append((start, pending))

    for lineno, line in logical:
        stripped = line.strip()
        if not re.match(r"(?i)^(copy|add)\s", stripped):
            continue
        parts = stripped.split()
        verb, operands = parts[0].upper(), parts[1:]
        flags = [p for p in operands if p.startswith("--")]
        if any(f.lower().startswith("--from=") for f in flags):
            continue
        operands = [p for p in operands if not p.startswith("--")]
        if len(operands) < 2:
            continue  # malformed, or a heredoc form; the daemon will say so
        sources, dest = operands[:-1], operands[-1]
        for src in sources:
            if src.startswith(("<<", '"<<')) or "$" in src:
                continue  # heredoc body, or built from an ARG this cannot resolve
            src = src.strip('"')
            if verb == "ADD" and re.match(r"(?i)^(https?|git)", src):
                continue
            if any(ch in src for ch in "*?["):
                if not list(context.glob(src)):
                    problems.append(
                        f"{label}: Dockerfile:{lineno} {verb}s {src!r} and nothing "
                        f"in the build context matches it")
                continue
            if not (context / src).exists():
                hint = ""
                near = [p.name for p in context.iterdir()
                        if p.name != src
                        and (p.suffix and p.suffix == Path(src).suffix
                             or p.stem == Path(src).stem)]
                if near:
                    hint = f" (context has {', '.join(sorted(near)[:3])})"
                problems.append(
                    f"{label}: Dockerfile:{lineno} {verb}s {src!r} to {dest!r} but "
                    f"the build context has no such path{hint}; this image cannot "
                    f"build")
    return problems


def _collected_output_loss(task_dir: Path, evaluation: "config.Evaluation",
                           notes: list[str]) -> list[str]:
    """No ``[[artifacts]] exclude`` pattern may match a name State B must produce.

    The complementary case to :func:`_collected_fixture_loss`, and invisible to it.
    That function compares the exclude list against ``original.tar.gz``, so it can
    only see a pattern that hits something State A *ships*.  A pattern can instead
    hit something a correct submission is *required to create*, in which case
    pristine State A holds no match, the check passes, and an identity run passes
    too -- identity builds both sides from the same tarball, which is not what such
    a pattern is aimed at.

    lang06 is the measured instance.  ``jsonnet`` and ``jsonnetfmt`` were excluded
    as State A's linked ELF binaries at the repository root, built by ``make``.
    They are also the two assembly names ``behavioural/suite.toml`` declares under
    ``programs``, and the shortest layout satisfying that contract names its project
    directories after them -- ``src/jsonnet/jsonnet.csproj``, which instruction.md
    blesses by name.  Harbor builds one ``tar --exclude=`` flag per entry and a bare
    GNU tar pattern matches by basename at any depth, so on 2026-08-07 three of six
    runs had both CLI projects deleted between the agent phase ending and grading
    starting: 0 of 2609 behavioural checks, ``build/build/discovery`` reporting
    ``metadata.projects: []``.  The other three capitalised their directories and
    passed 160 to 179 of 2611.  It decided one published score outright -- the run
    that cleared stage 1 and then had no CLI to publish -- destroyed three stage 2
    measurements, and left two stage 1 reviews grounded in a repository missing the
    projects they were asked about, one of them saying so in its own summary.
    Nothing in any artifact records that a directory was removed.

    Anchoring does not fix it and was measured rather than assumed: under GNU tar
    1.34 ``--exclude=./jsonnet`` spares ``./src/jsonnet/`` but still prunes a
    root-level ``./jsonnet/``, and ``--exclude=/jsonnet`` matches nothing at all.
    Only a list that never names a required output is safe, which is what this
    checks.

    ``programs`` is a list of names in the one task that declares names; elsewhere
    it is a count (pf03 ``programs = 8``, build01 ``corpus_programs = 80``), so
    anything not a list of strings is not a claim about names and is skipped.
    """
    problems: list[str] = []
    task_toml = task_dir / "task.toml"
    if not task_toml.is_file():
        return problems
    try:
        declared = tomlcompat.load(task_toml)
    except (OSError, ValueError) as exc:
        return [f"task.toml could not be parsed to check artifact exclusions "
                f"against required outputs: {exc}"]

    patterns: list[str] = []
    for block in declared.get("artifacts") or []:
        patterns.extend(block.get("exclude") or [])
    if not patterns:
        return problems

    suite_rel = evaluation.stage("behavioural").options.get("suite")
    suite_path = evaluation.resolve(suite_rel or "behavioural/suite.toml")
    if not suite_path.is_file():
        return problems
    try:
        suite_raw = tomlcompat.load(suite_path)
    except (OSError, ValueError) as exc:
        return [f"{suite_path.name} could not be parsed to check artifact "
                f"exclusions against required outputs: {exc}"]

    # Collected wherever it is declared: the field sits under whichever table the
    # suite puts its build contract in, and a task is free to move it.
    required: list[str] = []

    def _harvest(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("programs", "required_programs", "assemblies"):
                    if isinstance(value, list) and all(
                            isinstance(v, str) for v in value):
                        required.extend(value)
                else:
                    _harvest(value)
        elif isinstance(node, list):
            for item in node:
                _harvest(item)

    _harvest(suite_raw)
    if not required:
        return problems

    hits: dict[str, list[str]] = {}
    for name in sorted(set(required)):
        for pattern in patterns:
            if fnmatch.fnmatch(name, pattern):
                hits.setdefault(pattern, []).append(name)
                break

    for pattern, names in sorted(hits.items()):
        problems.append(
            f"[[artifacts]] excludes {pattern!r}, which matches the required "
            f"program name(s) {', '.join(repr(n) for n in names)} that "
            f"{suite_path.name} declares; a submission whose project directory or "
            f"output file is called that has it deleted at collection, and every "
            f"stage then reports a submission that produces nothing")
    if not hits:
        notes.append(f"{len(patterns)} artifact exclusion(s), none matching any of "
                     f"the {len(set(required))} required program name(s)")
    return problems


def _collected_fixture_loss(task_dir: Path, env_dir: Path,
                            notes: list[str]) -> list[str]:
    """No ``[[artifacts]] exclude`` pattern may match a path State A tracks.

    The list's job is to drop run products, and every entry is written with one in
    mind.  The failure is that a pattern aimed at a product also describes a
    fixture: fw07 excluded ``*.osm.pbf`` for a downloaded extract and thereby
    dropped ``core/files/andorra.osm.pbf``, which its own graded config names as
    ``datareader.file`` under the submission's tree.  The submission then arrived
    without the file it was about to be told to read, could not import a graph, and
    never bound a port -- it passed 0.2904 of the stage's weighted checks, for a
    tree that was byte-identical to the reference everywhere else.  Stage 2 is
    all-or-nothing, so that rate and a tree that never compiled are paid the same
    nothing, and the port cannot reach stage 3 either.

    Nothing downstream can catch it.  An identity run builds both sides from the
    same tarball, fixtures and all, so it never sees a collected tree; the stage
    reports a submission that would not start, which is exactly what it should
    report if the submission were at fault.  The grader-side cause is invisible in
    the one artifact anybody reads.

    Checked here because this is the only place that holds both files at once: the
    exclude list is in ``task.toml`` and the tracked set is in
    ``original.tar.gz``, and the contradiction exists only between them.  Matching
    is per path segment against basenames, which is what the lists themselves
    assume -- they mix directory names (``target``, ``.git``) with extension globs
    (``*.class``) and no task writes an anchored path.  A collector that is
    narrower than this makes the check strict rather than wrong: it fails a task
    over a pattern that might not fire, and the fix for that is to write a pattern
    that cannot.
    """
    problems: list[str] = []
    task_toml = task_dir / "task.toml"
    tarball = env_dir / "original.tar.gz"
    if not task_toml.is_file() or not tarball.is_file():
        return problems

    try:
        declared = tomlcompat.load(task_toml)
    except (OSError, ValueError) as exc:
        return [f"task.toml could not be parsed to check artifact exclusions: {exc}"]

    patterns: list[str] = []
    for block in declared.get("artifacts") or []:
        patterns.extend(block.get("exclude") or [])
    if not patterns:
        return problems

    try:
        with tarfile.open(tarball) as tf:
            # The tarball holds one top-level directory (the repository); paths are
            # compared as the collector would see them, relative to its root.
            tracked = [m.name.split("/", 1)[1]
                       for m in tf.getmembers() if m.isfile() and "/" in m.name]
    except (OSError, tarfile.TarError) as exc:
        return [f"environment/original.tar.gz could not be read to check "
                f"{len(patterns)} artifact exclusion(s): {exc}"]

    # Grouped by pattern rather than by path: the fix is always to change one
    # pattern, and a task that loses two hundred files to one glob should read as
    # one problem with a count, not two hundred problems.
    hits: dict[str, list[str]] = {}
    for path in tracked:
        for segment in path.split("/"):
            hit = next((p for p in patterns
                        if fnmatch.fnmatch(segment, p)), None)
            if hit is not None:
                hits.setdefault(hit, []).append(path)
                break

    for pattern, paths in sorted(hits.items()):
        shown = ", ".join(sorted(paths)[:4])
        more = f", and {len(paths) - 4} more" if len(paths) > 4 else ""
        problems.append(
            f"[[artifacts]] excludes {pattern!r}, which matches {len(paths)} "
            f"path(s) State A tracks ({shown}{more}); collection would hand every "
            f"grading stage a submission missing files the original repository "
            f"ships, and a stage that reads one reports the submission as broken")
    if not hits:
        notes.append(f"{len(patterns)} artifact exclusion(s), none matching any of "
                     f"the {len(tracked)} paths State A tracks")
    return problems


def _ladder_budget(task_dir: Path, evaluation: config.Evaluation,
                   notes: list[str]) -> list[str]:
    """Harbor's ceiling for the verifier phase has to cover the ladder inside it.

    Two files and one relation between them, stated in ``docs/SCHEMA.md`` and
    spelled out arithmetically in nineteen of the twenty ``task.toml`` comments:
    Harbor gives the verifier phase ``[verifier].timeout_sec``, and the three
    stages of ``evaluation.toml`` declare what they spend out of it.

    ``[verifier]`` is Harbor's, and Harbor is not vendored here.
    ``[stages.*].timeout_sec`` is in *this* schema's file, which Harbor does not
    read; ``ladder`` applies it as one stage container's wall clock, and nothing
    applies the sum -- see the note on that field in ``docs/SCHEMA.md`` for why it
    is a hang detector rather than a quota.  So the failure worth catching here is
    drift between the two, and this command is the only place that holds both
    files at once.

    pf03 is what a ceiling nobody re-derived looks like, though not a violation:
    73800s of ladder under 75600, so 1800s of margin where the other nineteen
    leave 5400 or more, and fw05 carries 79200 for the identical 73800s ladder.
    Its comment claimed twenty-one hours and then decomposed them into
    1.5 + 4 + 6 = 11.5h.  Both numbers have been in the file since the task was
    written, so this is not drift that happened; it is the arithmetic never having
    been done, which is the same thing arriving by a different route and is what
    the reported margin is for.

    What the inequality itself buys is the case none of the twenty is in: a run
    that spent its stage budgets under a short ceiling would be killed by Harbor
    partway through a stage 3 with fifteen hours declared, and a stage with no
    result file is scored as a harness error -- sixty points never contested and
    a re-run of a twenty-one-hour verifier phase.

    Only the inequality is checked, never the size of the margin.  How much a task
    needs above the sum is a judgement about its own image builds, and a validator
    that demanded a number would be inventing policy this schema does not state.
    The margin is reported instead, which is what makes an 1800 sitting beside
    nineteen 5400s something a reader can see.
    """
    problems: list[str] = []
    task_toml = task_dir / "task.toml"
    if not task_toml.is_file():
        return problems                     # reported as missing by the caller

    try:
        declared = tomlcompat.load(task_toml)
    except (OSError, ValueError) as exc:
        return [f"task.toml could not be parsed to check the ladder budget: {exc}"]

    # Disabled stages spend nothing, so they are not in the sum -- but a stage
    # that declares no timeout is in it at the loader's default, because that is
    # what its author is relying on.
    #
    # Ladder order, not the dict's: sorted alphabetically this reads
    # "54000 + 10800 + 5400", which is the ladder backwards and the reverse of the
    # arithmetic written in the task.toml comment a reader is comparing it to.
    spending = {name: evaluation.stage(name).timeout_sec
                for name in result.STAGES
                if name in evaluation.stages and evaluation.stage(name).enabled}
    ladder = sum(spending.values())
    shape = " + ".join(f"{v:g}" for v in spending.values())

    verifier = declared.get("verifier")
    if not isinstance(verifier, dict) or "timeout_sec" not in verifier:
        return [f"task.toml declares no [verifier].timeout_sec, so nothing can "
                f"tell whether the {ladder:g}s of stage budget this task declares "
                f"fits inside the phase Harbor gives it"]

    # Typed, not converted, for the reason `config._number` gives: `float("41400")`
    # succeeds and hides the quotes, and `float(True)` is 1.0, so a bool here would
    # be reported as a one-second phase 37799s short of its ladder -- a true
    # sentence about an absurd cause.  This field is Harbor's, and this reader
    # cannot promise Harbor is as forgiving as `float` about either one.  The check
    # is inline rather than a call to `_number` because that raises, and `validate`
    # reports every problem in a task at once instead of dying on the first.
    ceiling_raw = verifier["timeout_sec"]
    if isinstance(ceiling_raw, bool) or not isinstance(ceiling_raw, (int, float)):
        return [f"task.toml [verifier]: timeout_sec is {ceiling_raw!r}, which is "
                f"not a number of seconds"]
    ceiling = float(ceiling_raw)

    if ceiling < ladder:
        problems.append(
            f"[verifier].timeout_sec is {ceiling:g}s but the stages declare "
            f"{shape} = {ladder:g}s, which is {ladder - ceiling:g}s more than the "
            f"phase they run in; Harbor would kill the verifier mid-ladder, and a "
            f"stage with no result file costs its points and a re-run of the whole "
            f"phase")
    else:
        notes.append(f"ladder budget {shape} = {ladder:g}s inside a "
                     f"{ceiling:g}s verifier phase ({ceiling - ladder:g}s spare "
                     f"for image builds and overhead)")
    return problems


#: Numerals an instruction writes out as words when counting adversaries.  Up to
#: twelve because twelve is a count this benchmark has used: lang07's toml
#: explains its six as "rather than the twelve a since-removed sibling task
#: used".  Past that a task would be writing digits, and a task that writes
#: "sixteen adversaries" is not reported -- which is the rule missing something
#: rather than accusing a correct file, and is the direction to be wrong in.
_WORD_NUMERALS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                  "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                  "eleven": 11, "twelve": 12}


def _figure(raw: str) -> float:
    """A number as an instruction writes it: ``1,328`` and ``40`` and ``60.0``."""
    return float(raw.replace(",", ""))


def _same(a: float, b: float) -> bool:
    """Whether two figures are the same number, one written as prose.

    ``behavioural_points`` is ``40.0`` in the toml and "40" in the prose, and a
    task that priced a stage at 12.5 would write "12.5" -- so this compares the
    numbers, not their spellings, at a tolerance far below any figure the ladder
    uses.
    """
    return abs(a - b) < 1e-9


def _instruction_arithmetic(task_dir: Path, evaluation: config.Evaluation,
                           notes: list[str]) -> list[str]:
    """Ladder figures quoted in ``instruction.md`` against ``[scoring]``.

    The instruction is the only graded artifact an agent reads and the only one no
    code consumes, so nothing else holds it against the toml beside it.  Retuning
    the ladder is a two-line edit to ``[scoring]`` and the prose does not follow
    it: a task left telling an agent that stage 3 is "where 25 of the 100 points
    are", or quoting a per-adversary figure whose product is not what the stage
    pays, is sending that agent to spend its budget against a stage worth a
    fraction of what it will actually get, and no other check in this file can
    see it.

    Prose cannot be checked in general, so this checks the shapes the corpus
    actually uses, and only where one is present:

    * ``N of the 100 points`` -- a stage's share of the whole.  ``N`` has to be a
      figure ``[scoring]`` pays: a stage total, or the max itself.
    * ``all 40 of its 40`` -- the full-marks entry condition for stage 3.  Both
      halves are the same number by construction, and that number is
      ``behavioural_points``: stage 2 is all-or-nothing, so passing every scored
      check is the same event as being paid the stage in full.
    * ``Six independent adversaries`` -- a count, in words or digits, inside a
      paragraph that is about the verification stage.  It is
      ``verification_models``.  The noun has to be one being counted:
      lang05 names its stages in a table as "3 verification", and reading that
      adjective as a count reported a task whose prose was right.
    * ``worth 10 points, for 60`` -- a per-adversary figure multiplied out.  The
      product is ``verification_points``.

    Six of the twenty tasks state at least one of these, and every rule above fires
    on at least one of them: build02 the share, lang02/lang05/pf02 the entry
    condition, fw05 and lang07 the count, fw05 the product.  The other fourteen say
    nothing about the ladder's numbers and pass without a check being made, which
    is the honest outcome for a file that makes no claim rather than a check that
    cannot fail.  So a ladder retuned without the prose following it is reported
    rather than shipped -- for whichever tasks chose to quote it.

    What this cannot do is notice an instruction that says nothing, or one that
    describes the ladder in a shape no task has used yet.  Both are why the count
    of tasks making a checkable claim is reported as a note.
    """
    problems: list[str] = []
    path = task_dir / "instruction.md"
    if not path.is_file():
        return problems                     # reported as missing by the caller

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"instruction.md could not be read to check its ladder "
                f"figures: {exc}"]

    policy = evaluation.scoring
    stage_totals = {policy.behavioural_points, policy.verification_points}
    # Bold and emphasis are markup, not part of a number: the corpus writes
    # "**Six**" and "**60**", and a rule that had to spell the asterisks would
    # miss the same sentence written plainly.
    flat = text.replace("*", "").replace("`", "")
    claims = 0

    # --- a stage's share of the whole ------------------------------------- #
    for match in re.finditer(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s+of\s+(?:the\s+)?"
                             r"([0-9][0-9,]*(?:\.[0-9]+)?)\s+points", flat):
        part, whole = _figure(match.group(1)), _figure(match.group(2))
        if not _same(whole, policy.max_score):
            continue                        # a count of test cases, not points
        claims += 1
        if not any(_same(part, t) for t in stage_totals) \
                and not _same(part, policy.max_score):
            problems.append(
                f"instruction.md says {match.group(1)} of the {match.group(2)} "
                f"points, but [scoring] pays {policy.behavioural_points:g} for "
                f"stage 2 and {policy.verification_points:g} for stage 3; an "
                f"agent is being told a stage is worth what it is not")

    # --- the full-marks entry condition ---------------------------------- #
    for match in re.finditer(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s+of\s+"
                             r"(?:its\s+|all\s+)?([0-9][0-9,]*(?:\.[0-9]+)?)\b",
                             flat):
        lo, hi = _figure(match.group(1)), _figure(match.group(2))
        # Only the equal pair, and only where the sentence is about the gate:
        # "1200 of 1277" is a pass count and "13 of 35" is an inventory.
        if not _same(lo, hi):
            continue
        window = flat[max(0, match.start() - 200):match.end() + 200].lower()
        if "full marks" not in window and "stage 3" not in window:
            continue
        claims += 1
        if not _same(lo, policy.behavioural_points):
            problems.append(
                f"instruction.md states the stage-3 entry condition as "
                f"{match.group(1)} of {match.group(2)}, but stage 2 pays "
                f"{policy.behavioural_points:g} and full marks is all of it")

    # --- how many adversaries -------------------------------------------- #
    want = policy.verification_models
    for para in re.split(r"\n\s*\n", flat):
        if "adversar" not in para.lower():
            continue
        for match in re.finditer(r"\b(?:([0-9]+)|(" + "|".join(_WORD_NUMERALS)
                                 + r"))\s+(?:independent\s+)?"
                                 r"(?:adversaries|adversary|models)\b",
                                 para, re.I):
            digits, word = match.group(1), match.group(2)
            count = float(digits) if digits else _WORD_NUMERALS[word.lower()]
            claims += 1
            if not _same(count, want):
                problems.append(
                    f"instruction.md says {match.group(0)!r}, but [scoring] "
                    f"declares verification_models = {want}; every adversary the "
                    f"prose invents or drops is "
                    f"{policy.points_per_survived_model:g} points an agent is "
                    f"budgeting against wrongly")

    # --- a per-adversary figure multiplied out --------------------------- #
    per = policy.points_per_survived_model
    for match in re.finditer(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s+points?,\s+for\s+"
                             r"([0-9][0-9,]*(?:\.[0-9]+)?)", flat):
        each, total = _figure(match.group(1)), _figure(match.group(2))
        if not _same(each, per):
            continue                        # not the per-adversary sentence
        claims += 1
        if not _same(total, policy.verification_points):
            problems.append(
                f"instruction.md multiplies {match.group(1)} points per "
                f"adversary out to {match.group(2)}, but [scoring] pays "
                f"{policy.verification_points:g} for stage 3 "
                f"({want} x {per:g} = {want * per:g})")

    if claims:
        notes.append(f"instruction.md quotes {claims} ladder figure(s), all "
                     f"agreeing with [scoring]" if not problems else
                     f"instruction.md quotes {claims} ladder figure(s)")
    else:
        notes.append("instruction.md quotes no ladder figures, so none were "
                     "checked against [scoring]")
    return problems


def _stage_layout(task_dir: Path, evaluation: config.Evaluation) -> list[str]:
    """Check this task's enabled stage directories are laid out like every other.

    Each declared stage context must carry a ``.dockerignore`` -- every stage
    Dockerfile ends in ``COPY . /tests/<stage>``, so without one a stray
    ``__pycache__`` from a local run is baked into the graded image, and which
    files are in it then depends on who built it.

    Subdirectories are held to ``lib`` (importable code), ``modules`` (the graded
    units) and ``data`` (inputs that are only read).  A payload sitting loose in
    the stage root, or a ground truth directory named differently in each task,
    is how the tree drifted before: one task called it ``reference``, two called
    it ``data``, and a fourth left a tarball at the top level.
    """
    problems: list[str] = []
    for name, stage in sorted(evaluation.stages.items()):
        if not stage.enabled:
            continue
        context = str(stage.options.get("context") or "")
        if not context:
            continue
        cdir = evaluation.resolve(context)
        if not cdir.is_dir():
            continue  # _stage_images already reports this
        rel = cdir.relative_to(task_dir)
        if not (cdir / ".dockerignore").is_file():
            problems.append(
                f"{rel}/.dockerignore is missing; the Dockerfile copies this "
                f"whole directory into the image, so a local __pycache__ would "
                f"ship with it")
        for child in sorted(cdir.iterdir()):
            if child.is_dir() and child.name not in _STAGE_DIRS:
                problems.append(
                    f"{rel}/{child.name}/ is not one of "
                    f"{'/'.join(sorted(_STAGE_DIRS))}; code belongs in lib/ or "
                    f"modules/ and read-only inputs in data/")
        loose = [c.name for c in sorted(cdir.iterdir())
                 if c.is_file()
                 and c.suffix in (".tar", ".gz", ".zip", ".jar", ".bin")]
        if loose:
            problems.append(
                f"{rel}/ has payload(s) in the stage root ({', '.join(loose)}); "
                f"they belong in {rel}/data/")
    return problems


def cmd_audit(args: argparse.Namespace) -> int:
    evaluation, task_dir = _resolve_for(args, "audit")
    stage = evaluation.stage("audit")
    out = evaluation.result_path("audit")

    if not stage.enabled:
        res = result.StageResult(stage="audit", task=evaluation.task,
                                 status="skip")
        res.note("stage disabled in evaluation.toml")
        res.write(out)
        _log(f"audit disabled; wrote {out}")
        return 0

    original = Path(args.original)
    repo = Path(args.repo)
    for path, label in ((original, "original tree"), (repo, "submission")):
        if not path.exists():
            res = result.StageResult.failed(
                "audit", evaluation.task,
                f"{label} not found at {path}")
            res.write(out)
            return _die(f"{label} not found at {path}")

    prompt = evaluation.resolve(
        stage.options.get("prompt") or "audit/prompt.txt")
    log_dir = _log_dir(evaluation, "audit", args)

    # The read-only scan, if the task declares one.  It runs before the review
    # and hands it a digest: mechanical observations are good at saying where to
    # look and bad at deciding what was found, so they inform the reviewer
    # instead of scoring. Its checks join the result as advisory, and a scan that
    # breaks is a note in the report rather than a failed gate -- the gates are
    # the reviewer's, and the reviewer can read the tree itself.
    scan_result = None
    findings = ""
    scan_rel = stage.options.get("scan")
    if scan_rel:
        scan_path = evaluation.resolve(scan_rel)
        try:
            scanner = scan.build(scan_path, original, repo,
                                 _stage_work(args, "scan"), _log)
            scan_result = scanner.run()
            # Units as well as checks: a scan module that died wrote no checks, so
            # the digest cannot infer it from them, and its silence would render as
            # a clean tree.
            findings = scan.digest(scan_result.checks, scan_result.units)
            _log(f"scan: {len(scan_result.checks)} observation(s), "
                 f"{sum(1 for c in scan_result.checks if c.verdict == 'fail')} flagged")
        except (FileNotFoundError, config.ConfigError) as exc:
            result.StageResult.failed("audit", evaluation.task,
                                      f"scan suite: {exc}").write(out)
            return _die(f"scan suite: {exc}", CONFIG_ERROR)
        except Exception as exc:                 # a broken scan must not gate
            traceback.print_exc()
            findings = (f"(the scan did not complete: {type(exc).__name__}: {exc}. "
                        f"Read the trees yourself; nothing below depends on it.)")
            _log(f"scan: did not complete ({exc}); the review continues without it")

    try:
        engine = audit.build(evaluation.task, stage, prompt, original, repo,
                                log_dir=log_dir, findings=findings)
    except (FileNotFoundError, config.ConfigError) as exc:
        result.StageResult.failed("audit", evaluation.task, str(exc)).write(out)
        return _die(str(exc), CONFIG_ERROR)

    default_model = str(stage.options.get("model") or audit.DEFAULT_MODEL)
    _log(f"audit: {engine.samples} review(s) of {evaluation.task} "
         f"with {args.model or default_model}")

    def factory(_index: int) -> models.Driver:
        return _driver_for(stage.options, args, default_model)

    try:
        res = engine.run(factory)
    except Exception as exc:                     # a broken stage, not a verdict
        traceback.print_exc()
        result.StageResult.failed("audit", evaluation.task,
                                  f"{type(exc).__name__}: {exc}").write(out)
        return _die(f"the audit stage did not complete: {exc}")

    if scan_result is not None:
        res.checks.extend(scan_result.checks)
        res.units.extend(scan_result.units)
        res.metadata["scan"] = scan.summary(scan_result)

    res.write(out)
    required = [c for c in res.checks if c.required]
    failed = [c.id for c in required if c.verdict == "fail"]
    undecided = [c.id for c in required if c.verdict == "error"]
    _log(f"audit: {sum(1 for c in required if c.verdict == 'pass')}/"
         f"{len(required)} required gate(s) passed"
         + (f"; failed: {', '.join(failed)}" if failed else "")
         + (f"; undecided: {', '.join(undecided)}" if undecided else ""))
    _log(f"wrote {out}")
    return 0


def cmd_behavioural(args: argparse.Namespace) -> int:
    evaluation, task_dir = _resolve_for(args, "behavioural")
    stage = evaluation.stage("behavioural")
    out = evaluation.result_path("behavioural")

    if not stage.enabled:
        res = result.StageResult(stage="behavioural", task=evaluation.task,
                                 status="skip")
        res.note("stage disabled in evaluation.toml")
        res.write(out)
        return 0

    suite_path = evaluation.resolve(stage.options.get("suite")
                                    or "behavioural/suite.toml")
    try:
        suite = config.Suite.load(suite_path)
    except config.ConfigError as exc:
        result.StageResult.failed("behavioural", evaluation.task, str(exc)).write(out)
        return _die(str(exc), CONFIG_ERROR)

    repo = Path(args.repo)
    if not repo.exists():
        result.StageResult.failed("behavioural", evaluation.task,
                                  f"submission not found at {repo}").write(out)
        return _die(f"submission not found at {repo}")

    work = Path(args.work or "/tmp/swerefactor-behavioural")
    work.mkdir(parents=True, exist_ok=True)
    original = Path(args.original) if Path(args.original).exists() else None

    # SuiteRunner's fifth argument is the log *callable*, not a log directory.
    # This stage produces no model transcript, so unlike stages 1 and 3 it has
    # nothing to put in --log-dir: each module's stdout is captured under the
    # work directory and its tail is folded into the unit summary, which is what
    # survives in the result JSON.
    _log(f"behavioural: {len(suite.modules)} module(s) for {evaluation.task}")
    # Written after every module, not only at the end.  This stage is killed from
    # outside at its own timeout (`ladder.run_stage` -> `docker kill`), and the
    # single write below is unreachable once that lands, so ten measured modules
    # were being discarded to record the eleventh's overrun.  The partial file is a
    # complete description of the suite at every instant -- see `SuiteRunner.run`'s
    # seeding of `unreached` -- so a reader who finds one after a kill can tell what
    # was measured from what the clock never reached.
    runner = behavioural.SuiteRunner(suite, repo, original, work, _log,
                                   checkpoint=lambda r: r.write(out))
    try:
        res = runner.run()
    except Exception as exc:
        traceback.print_exc()
        result.StageResult.failed("behavioural", evaluation.task,
                                  f"{type(exc).__name__}: {exc}").write(out)
        return _die(f"the behavioural stage did not complete: {exc}")

    res.write(out)
    for unit in res.units:
        checks = res.checks_of(unit.id)
        # `pooled` returns earned and total *weight*, not a count, and the two
        # differ for every module whose checks are not all weight 1.0: `prepare`
        # carries five checks summing to 6.0 weight, `platform` four summing to
        # 6.0.  Printing that as "6/6 checks" invents a number that appears
        # nowhere in the artifact and contradicts the module's own stdout
        # ("[prepare] 5/5 passed") -- a disagreement inside one report, where the
        # wrong side reads like a measurement.  Licensed skips pull the other way
        # (`sourcemaps`: 291 checks, 287 scored weight), so the label was wrong in
        # both directions at once.  `%g` rather than `%.0f` for the same reason: a
        # partial module earns fractional weight and rounding hides it.
        passed, total = result.pooled(checks)
        # Every number on this line is named, because two separate branches found
        # it ambiguous from opposite ends.  It once printed a weight sum under the
        # word "checks" -- "0/10 checks" for a module of 4, "0/90" for a module of
        # 36, directly under that module's own truthful "0/4 passed" -- and a
        # reader had no way to tell which number was which.
        #
        # `pooled` now counts checks rather than summing weight, so the two pairs
        # differ only in the denominator, and that difference is the whole reason
        # to print both: the first is over every check the module recorded, the
        # second over the ones that score -- which since skips are charged means
        # they differ only where a module carries weight-0 observation checks.
        #
        # The third number is the skips, and it is labelled "did not run" rather
        # than "unscored" because they now score: they are in `total` and none of
        # them is in `passed`.  Printing them under the old word would have said
        # the opposite of what the denominator does.
        npass = sum(1 for c in checks if c.ok)
        unrun = sum(1 for c in checks if not c.judged)
        _log(f"  {unit.id:<20} {unit.status:<8} "
             f"rate {result.rate(checks):.4f}  "
             f"{npass}/{len(checks)} check(s) passed, "
             f"{passed:g}/{total:g} scored"
             + (f", {unrun} did not run (charged 0)" if unrun else "")
             + f"  (module weight {unit.weight:g})")
    _log(f"wrote {out}")
    return 0


def cmd_verification(args: argparse.Namespace) -> int:
    evaluation, task_dir = _resolve_for(args, "verification")
    if "verification" not in evaluation.stages:
        return _die("this task declares no [stages.verification]", CONFIG_ERROR)
    stage = evaluation.stage("verification")
    out = evaluation.result_path("verification")

    if not stage.enabled:
        res = result.StageResult(stage="verification", task=evaluation.task,
                                 status="skip")
        res.note("stage disabled in evaluation.toml")
        res.write(out)
        return 0

    # The ladder's own entry condition, enforced here rather than only at scoring
    # time.  `scoring.grade` refuses to *pay* for a stage 3 run that stage 2 did not
    # earn; without this nothing refuses to *run* one, and an operator driving the
    # stages by hand spends six adversary rounds -- six model runs against a built
    # tree -- to produce a file the scorer is guaranteed to discard.
    #
    # Both rungs are asked, stage 1 first.  A failed audit gate scores zero
    # whatever stage 2 measured, so the rounds are just as surely discarded -- and
    # stage 2 is the stage that needs no credentials, which makes "stage 1 failed,
    # stage 2 ran anyway" the ordinary shape of a hand-driven ladder rather than an
    # unlikely one.  Stage 1 is asked first because it is the earlier rung: an
    # operator whose gate failed should be told that, not told about a stage 2 that
    # could not have mattered.
    #
    # No result file is written on this path, and that is the whole subtlety.  A
    # `status="skip"` file would be read back by `_read_stage` and graded as
    # `status != "ok"`, i.e. as `verification-error`: a harness fault charged to a
    # submission whose only fault was stage 2.  Absent is the encoding the scorer
    # already has for this exact case -- `grade_verification`'s `expected` flag is
    # False whenever the rungs below did not pass, so an absent file here raises
    # nothing.  `_read_stage`'s own docstring says so: "Stage 3 has no file when
    # the ladder correctly stopped at stage 2."
    #
    # Exit 0, not 2.  Nothing is wrong with the task or the invocation; the
    # submission did not earn the stage.  A non-zero exit here would make every
    # driver script that runs the three stages in sequence report a failed run for
    # a ladder that worked exactly as specified.
    if not args.force:
        passed, why = scoring.audit_passes_gate(
            _read_stage(evaluation, "audit"), evaluation.scoring,
            evaluation.task)
        if passed:
            reached, _points, why = scoring.behavioural_reaches_gate(
                _read_stage(evaluation, "behavioural"), evaluation.scoring,
                evaluation.task)
        else:
            reached = False
        if not reached:
            _log(f"verification: not run -- {why}")
            _log("verification: the stage is asked only of a submission that "
                 "passed the audit gate and reached the stage-2 gate; no "
                 "result file written (--force overrides)")
            # An earlier run's file must not be left to stand as this run's answer.
            if out.exists():
                out.unlink()
                _log(f"verification: removed stale {out}")
            return 0

    probe_path = evaluation.resolve(stage.options.get("probe")
                                    or "verification/probe.toml")
    try:
        probe = config.Probe.load(probe_path)
    except config.ConfigError as exc:
        result.StageResult.failed("verification", evaluation.task, str(exc)).write(out)
        return _die(str(exc), CONFIG_ERROR)

    original, repo = Path(args.original), Path(args.repo)
    for path, label in ((original, "original tree"), (repo, "submission")):
        if not path.exists():
            result.StageResult.failed("verification", evaluation.task,
                                      f"{label} not found at {path}").write(out)
            return _die(f"{label} not found at {path}")

    work = Path(args.work or "/tmp/swerefactor-verification")
    log_dir = _log_dir(evaluation, "verification", args)
    judge_spec = dict(stage.options.get("adjudicator") or {})
    judge_model = str(judge_spec.get("model")
                      or stage.options.get("model")
                      or audit.DEFAULT_MODEL)
    try:
        judge = _driver_for(judge_spec, args, judge_model)
    except models.ModelError as exc:
        return _die(f"adjudicator unavailable: {exc}")

    try:
        engine = verification.build(probe, original, repo, work,
                                   adjudicator_driver=judge, log_dir=log_dir)
    except (FileNotFoundError, ValueError, OSError) as exc:
        result.StageResult.failed("verification", evaluation.task, str(exc)).write(out)
        return _die(str(exc))

    _log(f"verification: {len(probe.adversaries)} round(s) for {evaluation.task}")

    def factory(adv: config.Adversary) -> models.Driver:
        spec = {"driver": adv.driver, "model": adv.model,
                "driver_options": adv.options.get("driver_options") or {}}
        return _driver_for(spec, args, adv.model)

    try:
        res = engine.run(factory)
    except Exception as exc:
        traceback.print_exc()
        result.StageResult.failed("verification", evaluation.task,
                                  f"{type(exc).__name__}: {exc}").write(out)
        return _die(f"the verification stage did not complete: {exc}")
    finally:
        if not args.keep_work:
            engine.runner.cleanup()

    res.write(out)
    meta = res.metadata
    _log(f"verification: {meta.get('survived', 0)} survived, "
         f"{meta.get('broken', 0)} broke through, "
         f"{meta.get('errored', 0)} did not complete")
    for check in res.checks:
        _log(f"  {check.id:<24} {check.verdict:<6} {check.summary[:90]}")
    _log(f"wrote {out}")
    return 0


def _read_stage(evaluation: config.Evaluation, name: str) -> result.StageResult | None:
    """Read a stage result, or None if it was never written.

    Absent is not the same as failed.  Stage 3 has no file when the ladder
    correctly stopped at stage 2, and the scorer distinguishes the two by which
    stage stopped it -- not by which files exist.
    """
    if name not in evaluation.stages:
        return None
    path = evaluation.result_path(name)
    if not path.exists():
        return None
    try:
        return result.StageResult.read(path)
    except (OSError, ValueError) as exc:
        res = result.StageResult.failed(name, evaluation.task,
                                        f"result at {path} is unreadable: {exc}")
        return res


def cmd_score(args: argparse.Namespace) -> int:
    # Plain `_resolve`, unlike the three stage commands: a broken config here
    # exits 2 with a message and writes nothing.  That is deliberate.  All twenty
    # tasks grade through `swerefactor.harbor`, which publishes a `valid: 0` reward
    # on this path precisely because a graded run must leave a reward file; this
    # command is the operator's re-render of stages that already ran.  Writing a
    # verdict from a config it could not read would put a second fabricated zero
    # in the results directory, and the one that a consumer reads is harbor's.
    evaluation, task_dir = _resolve(args)
    audit = _read_stage(evaluation, "audit")
    behavioural_res = _read_stage(evaluation, "behavioural")
    verification_res = _read_stage(evaluation, "verification")

    # `declared` is what `_read_stage` above threw away.  It returns None both for
    # a stage the task never asked for and for one it asked for whose file is not
    # there, and only the scorer can tell those apart -- the first is the ladder
    # working, the second is 60 points that were never contested.  The information
    # exists here; passing it is the whole fix.
    verdict = scoring.grade(evaluation.task, evaluation.scoring,
                            audit, behavioural_res, verification_res,
                            declared=evaluation.stages)
    out = Path(args.out) if args.out else Path(evaluation.results_dir) / "score.json"
    verdict.write(out)

    text = report.render(verdict, evaluation.title or evaluation.task)
    print(text)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    _log(f"wrote {out}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    path = Path(args.verdict)
    if not path.exists():
        return _die(f"no verdict at {path}")
    try:
        raw = scoring.Verdict.read(path)
    except (OSError, ValueError) as exc:
        return _die(f"cannot read {path}: {exc}")
    print(report.render(raw, args.title or raw.task))
    return 0


def cmd_telemetry(args: argparse.Namespace) -> int:
    """Record what a run's grading models cost in time and tokens.

    Reads a ``<task>/<model>/<run>`` directory's ``score.json``, where each stage
    recorded the ``usage`` of the models it drove, and writes ``telemetry.json``
    beside it.  ``--stdout`` prints it instead, for a pipeline that wants the
    record without the file.

    Run after a run is graded: the input is not produced by this command, so a
    corpus can be back-filled one run at a time.
    """
    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        return _die(f"no run directory at {run_dir}")
    try:
        record = telemetry.for_run(run_dir)
    except FileNotFoundError as exc:
        return _die(str(exc))
    if args.stdout:
        print(json.dumps(record, indent=2, sort_keys=False))
    else:
        out = telemetry.write(run_dir)
        _log(f"wrote {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swerefactor",
        description="SWERefactorBench grading: three stages, one scorer.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, *, needs_repo: bool = True) -> None:
        p.add_argument("--task-dir", required=True,
                       help="the task directory containing tests/evaluation.toml")
        p.add_argument("--results", default="",
                       help=f"where stage results go (default from evaluation.toml, "
                            f"else {DEFAULT_RESULTS})")
        p.add_argument("--log-dir", default="",
                       help="where transcripts go (default: alongside results)")
        if needs_repo:
            p.add_argument("--repo", default=DEFAULT_REPO,
                           help=f"the submission under test (default {DEFAULT_REPO})")
            p.add_argument("--original", default=DEFAULT_ORIGINAL,
                           help=f"the frozen State A tree (default {DEFAULT_ORIGINAL})")

    def model_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--driver", default="",
                       help="override the driver: "
                            + ", ".join(sorted(models.DRIVERS)))
        p.add_argument("--model", default="", help="override the model id")
        p.add_argument("--script", default="",
                       help="with --driver scripted: a JSON file of turns to "
                            "replay, for testing a prompt without a model")

    p = sub.add_parser("validate", help="check a task's configuration")
    p.add_argument("--task-dir", required=True)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("audit", help="stage 1: the agentic audit review")
    common(p)
    model_args(p)
    p.add_argument("--work", default="",
                   help="scratch directory for the read-only scan")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("behavioural", help="stage 2: the behavioural modules")
    common(p)
    p.add_argument("--work", default="", help="scratch directory")
    p.set_defaults(func=cmd_behavioural)

    p = sub.add_parser("verification", help="stage 3: the verification rounds")
    common(p)
    model_args(p)
    p.add_argument("--work", default="", help="scratch directory for the trees")
    p.add_argument("--keep-work", action="store_true",
                   help="keep the copied trees, for debugging a round")
    p.add_argument("--force", action="store_true",
                   help="run the rounds even when stage 1 failed its gate or "
                        "stage 2 did not pass every check. For developing an "
                        "adversary against a tree that is not meant to pass: the "
                        "scorer still will not pay for the result, so a graded run "
                        "has no use for this")
    p.set_defaults(func=cmd_verification)

    p = sub.add_parser("score", help="apply the ladder to whatever stages ran")
    common(p, needs_repo=False)
    p.add_argument("--out", default="", help="where the verdict JSON goes")
    p.add_argument("--report", default="", help="also write the text report here")
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("report", help="re-render a verdict as text")
    p.add_argument("--verdict", required=True)
    p.add_argument("--title", default="")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("ladder",
                       help="run all three stage images as sibling containers "
                            "and score -- what a Harbor verifier calls")
    p.add_argument("--task-dir", default="/",
                   help="the task directory containing tests/evaluation.toml")
    p.add_argument("--results", default="",
                   help=f"where stage results go (default from evaluation.toml, "
                        f"else {DEFAULT_RESULTS})")
    p.add_argument("--repo", default=DEFAULT_REPO,
                   help=f"the submission under test (default {DEFAULT_REPO})")
    p.add_argument("--scratch", default="",
                   help="scratch for the unpacked State A "
                        "(default /tmp/swerefactor-ladder)")
    p.set_defaults(func=ladder.cmd_ladder)

    p = sub.add_parser("telemetry",
                       help="record what a run's grading models cost, as "
                            "telemetry.json")
    p.add_argument("run_dir",
                   help="a <task>/<model>/<run> directory holding score.json")
    p.add_argument("--stdout", action="store_true",
                   help="print the telemetry record instead of writing telemetry.json")
    p.set_defaults(func=cmd_telemetry)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except config.ConfigError as exc:
        return _die(str(exc), CONFIG_ERROR)
    except KeyboardInterrupt:
        return _die("interrupted")


if __name__ == "__main__":
    sys.exit(main())
