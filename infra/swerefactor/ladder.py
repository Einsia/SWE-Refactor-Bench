"""Driving the three stage images from inside one Harbor verifier.

Harbor runs one verifier per trial, in one container.  This benchmark grades in
three, and the split is load-bearing rather than packaging: stage 2 asserts at
image-build time that the old toolchain is absent, and a single image holding
both toolchains cannot make that assertion at all.  So the verifier Harbor
starts is not the grader here -- it is an orchestrator that drives the three
stage images as sibling containers over a mounted Docker socket, and then
applies the ladder to whatever they left behind.

The orchestrator therefore runs only code from this repository and never runs the
submission: driving sibling containers means holding a Docker socket, which is
host-level access, and that is the one privilege the split adds.  The submission
runs where it ran before -- inside a stage container, with no socket -- and stage 2
still runs with no network at all.

The other consequence of siblings is networking.  Harbor's network policy
applies to the containers Harbor starts, and a container started over the socket
is not one of them, so the driver sets each stage's network itself: ``none`` for
stage 2, because offline is a graded property of that stage, and a named network
for the two model-driven stages, which have to reach a gateway.

Run by hand exactly as Harbor runs it:

    python3 -m swerefactor ladder --task-dir /
"""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
from pathlib import Path

from . import config, harbor
from .result import StageResult

#: Where each stage image expects the submission.  Stage 1 is a reviewer's
#: workbench and documents ``/opt/workspace`` (docs/SCHEMA.md); stages 2 and 3
#: rebuild in place under ``/workspace/repo``, which is ``cli.DEFAULT_REPO``.
#: The difference is not cosmetic: each image creates only its own mount point,
#: and a driver that guessed one path for all three would hand stage 1 an empty
#: tree to review -- a full-marks verdict on nothing, which reads as a pass.
REPO_IN = {
    "audit": "/opt/workspace",
    "behavioural": "/workspace/repo",
    "verification": "/workspace/repo",
}

#: Stage 2 runs with no network because that is a graded property of the stage,
#: not a precaution: its image asserts at build time that the old toolchain is
#: gone, and a submission that fetched the old one at test time would defeat the
#: assertion.  The other two need a gateway to reach a model.
OFFLINE = "behavioural"

#: Forwarded into the two model-driven stages.  The driver does not read these
#: and cannot check them; a gateway is the operator's to supply, and a stage
#: whose key is missing says so in its own result.  ``SRB_FORWARD_ENV`` is the
#: escape hatch for a task that names a key variable of its own via the
#: ``api_key_env`` stage option -- none of the twenty do today.
GATEWAY_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_EXTRA_HEADERS",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_EXTRA_HEADERS",
)


def _log(message: str) -> None:
    print(f"[ladder] {message}", flush=True)


def _docker(*args: str) -> list[str]:
    """The docker CLI, with no host of its own.

    Inside the verifier the socket is bind-mounted at the default path, so plain
    ``docker`` is right.  On a host with several daemons -- a rootless one among
    them -- export ``DOCKER_HOST``; the CLI reads it and this driver does not
    need to know.
    """
    return ["docker", *args]


def _run(argv: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _copy_in(cid: str, src: Path, dest: str) -> None:
    """Put ``src`` into the container at exactly ``dest``.

    The tar is streamed into ``/`` with ``dest``'s root-relative path as the
    member name, so extraction creates whatever parents are missing and one
    recipe works for three images that each create only their own mount point.

    Copying into ``dest``'s parent instead -- under ``dest``'s basename -- is
    what this did first, and it requires that parent to already exist.
    ``docker cp`` does not create it: it reports ``destination ... must be a
    directory`` and closes the pipe.  Ten of the twenty tasks' behavioural images
    never create ``/workspace`` -- measured by probing each built image, since a
    grep for ``mkdir``/``WORKDIR`` counts neither the ``COPY`` destinations that
    also create it nor the parents a base image brought -- so for those tasks
    stage 2 could not be reached at all, and the run was scored NOT VALID with
    stage 1 passing.  (Seven of them are among the seventeen tasks this campaign
    graded; that count, against this denominator, is where "seven of the twenty"
    came from.)  A
    bind mount hides this -- Docker creates a mount point, which is why the
    by-hand recipe in tests/test.sh works where this driver did not.

    Streamed rather than built in memory: the submission is the largest thing
    this driver touches, and a copy of it in the verifier's RAM or its disk is a
    copy that a task's declared storage limit did not budget for.

    A premature exit by ``docker cp`` breaks the pipe mid-write.  That is caught
    rather than propagated so the failure is reported with docker's own stderr:
    ``BrokenPipeError`` escaping from inside the ``with`` block skips the read
    below, which replaces the cause with its symptom and reads as a transient
    contention flake.
    """
    proc = subprocess.Popen(_docker("cp", "-", f"{cid}:/"),
                            stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    broken = False
    try:
        with tarfile.open(fileobj=proc.stdin, mode="w|") as tf:
            tf.add(str(src), arcname=dest.lstrip("/"))
    except BrokenPipeError:
        broken = True
    finally:
        try:
            proc.stdin.close()
        except BrokenPipeError:
            broken = True
    err = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
    rc = proc.wait()
    # A broken pipe with rc=0 still means the archive was cut short, so the copy
    # is incomplete either way and neither condition alone is sufficient.
    if rc != 0 or broken:
        raise RuntimeError(f"docker cp into {dest} failed (rc={rc}"
                           f"{', pipe broken' if broken else ''}): "
                           f"{err or 'docker wrote no error'}")


def _copy_out(cid: str, src: str, dest: Path) -> None:
    """Pull ``src`` out of the container into ``dest``, which is a directory.

    Failure here is reported, not raised: a stage that ran and left nothing to
    collect is a stage with an empty result, and the scorer's job is to say so.
    Turning it into an exception would replace a graded zero with a harness
    error, which reads as the benchmark's fault rather than the submission's.
    """
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(_docker("cp", f"{cid}:{src}", "-"),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|*") as tf:
            for member in tf:
                # The archive is prefixed with src's basename; drop it so the
                # contents land in dest rather than a directory named after it.
                parts = member.name.split("/")[1:]
                if not parts or not parts[0]:
                    continue
                member.name = "/".join(parts)
                tf.extract(member, path=str(dest), set_attrs=False)
    except tarfile.TarError as exc:
        _log(f"warning: could not read {src} back out: {exc}")
    finally:
        proc.stdout.close()
    if proc.wait() != 0:
        err = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
        _log(f"warning: docker cp out of {src} failed: {err}")


def unpack_original(tarball: Path, into: Path) -> Path:
    """Unpack State A so that ``into`` *is* the repository root.

    The twenty tarballs do not agree on their shape: seventeen hold a single
    top-level directory (``repo/``, but also ``graphhopper/`` and
    ``jsonnet-0.20.0/``), and three are flat.  Each task's environment
    Dockerfile already encodes its own answer as a ``--strip-components`` it
    passes or omits, and copying those twenty answers into twenty test scripts
    would be twenty places for one to drift from the image it describes.

    So the shape is read off the archive instead: strip one component when every
    member sits under one common first segment, otherwise strip none.  That
    reproduces all twenty recipes, and a new task gets the right answer without
    declaring anything.
    """
    into.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball) as tf:
        members = tf.getmembers()
        roots = {
            [p for p in m.name.split("/") if p not in ("", ".")][0]
            for m in members
            if [p for p in m.name.split("/") if p not in ("", ".")]
        }
        strip = 1 if len(roots) == 1 else 0
        keep = []
        for m in members:
            parts = [p for p in m.name.split("/") if p not in ("", ".")]
            if len(parts) <= strip:
                continue
            m.name = "/".join(parts[strip:])
            keep.append(m)
        # No same-owner: the tarball's uids are the upstream release's, and the
        # trees this is compared against were unpacked without them too.
        tf.extractall(path=str(into), members=keep, numeric_owner=False)
    _log(f"State A unpacked to {into} ({len(members)} members, strip={strip})")
    return into


def _stage_env(name: str) -> list[str]:
    """``-e`` flags for one stage's container, by name only.

    ``-e VAR`` rather than ``-e VAR=value``: the second form puts an API key in
    the argument list of a docker process, where ``ps`` can read it and any log
    of the command records it.  The bare form makes the CLI read the value out
    of this process's own environment and pass it over the socket instead, so a
    key never appears in a command line.

    Stage 2 is given nothing at all.  It has no network to spend a key on, and
    the fewer things reachable from the stage that runs submitted code, the
    less there is for submitted code to find.
    """
    if name == OFFLINE:
        return []
    names = list(GATEWAY_ENV)
    extra = os.environ.get("SRB_FORWARD_ENV", "")
    names += [n for n in extra.replace(",", " ").split() if n]
    flags: list[str] = []
    for var in names:
        if var in os.environ:
            flags += ["-e", var]
    return flags


def _network_for(name: str) -> str:
    """Stage 2 is offline by definition; the others take what the operator says.

    Harbor's own network policy governs the containers Harbor starts, and these
    are not among them -- so this is the only place the two model-driven stages'
    reachability is decided.  ``none`` for stage 2 is not configurable, because
    a run that let stage 2 reach a network would be measuring something else.
    """
    if name == OFFLINE:
        return "none"
    return os.environ.get("SRB_STAGE_NETWORK", "bridge")


#: Which environment variable holds one driver's credential.  ``anthropic``
#: accepts either name because a gateway in front of the API usually calls it
#: ``AUTH_TOKEN``; the driver reads both, so the preflight has to accept both or
#: it would refuse a run that would have worked.
_KEY_ENV_BY_DRIVER: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "openai": ("OPENAI_API_KEY",),
}


def _credentials_needed(evaluation: config.Evaluation) -> dict[str, list[str]]:
    """Which key variables the enabled model-driven stages will ask for.

    Keyed by variable name, valued by what wants it, so the message can say
    *why* a key is needed rather than only that one is.  Read from the same
    fields the runners read: the audit stage's ``driver``, and stage 3's
    adversaries and adjudicator.

    Best-effort by construction.  A task naming its own variable via
    ``api_key_env`` is honoured, an unrecognised driver contributes nothing --
    ``command`` and ``scripted`` need no credential at all -- and an unreadable
    probe is skipped rather than raised, because refusing to start over a file
    stage 3 would have complained about itself is a worse failure than starting.
    """
    needed: dict[str, list[str]] = {}

    def want(driver: str, options: dict, who: str) -> None:
        named = str(options.get("api_key_env") or "")
        names = (named,) if named else _KEY_ENV_BY_DRIVER.get(driver, ())
        if not names:
            return
        # Any one of the alternatives satisfies the requirement, so they are
        # recorded under a single joined key and reported as a choice.
        needed.setdefault(" or ".join(names), []).append(who)

    audit = evaluation.stages.get("audit")
    if audit is not None and audit.enabled:
        opts = audit.options
        want(str(opts.get("driver") or ""), opts,
             f"stage 1 ({opts.get('model') or 'unnamed model'})")

    verification = evaluation.stages.get("verification")
    if verification is not None and verification.enabled:
        opts = verification.options
        adj = opts.get("adjudicator") or {}
        if isinstance(adj, dict) and adj.get("model"):
            want(str(adj.get("driver") or "anthropic"), adj,
                 f"stage 3 adjudicator ({adj['model']})")
        probe_rel = opts.get("probe")
        if probe_rel:
            try:
                probe = config.Probe.load(evaluation.resolve(str(probe_rel)))
            except (config.ConfigError, OSError):
                probe = None
            if probe is not None:
                for adv in probe.adversaries:
                    want(adv.driver, adv.options,
                         f"stage 3 adversary {adv.id} ({adv.model})")
    return needed


def _preflight_credentials(evaluation: config.Evaluation) -> str:
    """The missing-credential message, or empty when nothing is missing.

    Checked here rather than only at the first request because of *when* the
    failure would otherwise land.  Each driver does raise on an unset key, so a
    run without one was never scored zero -- but it discovered that after the
    orchestrator had started a stage container, copied State A and the
    submission in, and let the stage burn toward its own timeout, and the
    operator read the outcome as a stage that failed rather than as a key they
    forgot to export.  Stage 1 is the gate, so that is the whole ladder.

    Exactly as strict as the drivers are: same variables, same alternatives, and
    an endpoint that wants no credential is not a case either one allows.
    """
    needed = _credentials_needed(evaluation)
    missing = {
        names: why for names, why in needed.items()
        if not any(os.environ.get(n.strip()) for n in names.split(" or "))
    }
    if not missing:
        return ""
    lines = [
        f"{names} is unset, and {len(why)} model call(s) need it: "
        + ", ".join(sorted(why))
        for names, why in sorted(missing.items())
    ]
    return (
        "the model-driven stages have no credentials, so the ladder was not "
        "started. Grading one task needs a key for every driver its stages "
        "name -- stage 1 is a gate, and stage 3 runs six adversaries and an "
        "adjudicator, which need not be the same vendor as stage 1. "
        + ". ".join(lines)
        + ". Export them and re-run; stage 2 needs none and runs offline."
    )


def run_stage(stage: config.StageConfig, *, eval_path: Path, repo: Path,
              original: Path | None, results: Path) -> tuple[int, str]:
    """Drive one stage image as a sibling container.

    Returns its exit status and a one-line note.  A non-zero status is not by
    itself a harness failure: stage 3 exits non-zero when it declines to run
    because stage 2 fell short of the gate, and that is the ladder working.  The
    verdict lives in the result file, so this reports the status and lets the
    scorer read what the stage actually wrote.
    """
    image = str(stage.options.get("image") or "")
    if not image:
        return 1, f"[stages.{stage.name}] declares no image"
    if _run(_docker("image", "inspect", image)).returncode != 0:
        return 1, (f"image {image} is not present; build it with "
                   f"`docker build -t {image} tasks/<task>/tests/"
                   f"{stage.options.get('context') or stage.name}`")

    repo_in = REPO_IN[stage.name]
    created = _run(_docker(
        "create", "--network", _network_for(stage.name), "--user", "0:0",
        *_stage_env(stage.name), image,
        "python3", "-m", "swerefactor", stage.name,
        "--task-dir", "/", "--repo", repo_in, "--original", "/opt/original",
        "--results", "/logs/verifier"))
    if created.returncode != 0:
        return 1, f"docker create failed: {created.stderr.strip()}"
    cid = created.stdout.strip()

    try:
        # evaluation.toml at /tests/, so `--task-dir /` finds it and every
        # relative path in it -- the suite, the prompt, the probe -- resolves
        # against /tests, which is where the image already put its own context.
        _copy_in(cid, eval_path, "/tests/evaluation.toml")
        _copy_in(cid, repo, repo_in)
        # The reference tree is a mount some stages need and at least one forbids.
        # fw04 and fw06 read it at /opt/original and their images do not carry one,
        # so for them the copy is the mount.  lang07's image asserts the path is
        # *absent* (`test ! -d /opt/original` in its final layer) because the whole
        # point of that task is that the reference cannot be reached from the
        # container the submission's own build runs in.  That assertion is build
        # time, so copying one in at grade time is not something the image can
        # catch -- which is exactly why the opt-out below is the mechanism rather
        # than a check.  A stage that does not want the
        # reference declares `reference = false`; the driver then resolves an absent
        # --original to None, which is the same optional-mount state fw07 relies on.
        if original is not None and stage.options.get("reference", True):
            _copy_in(cid, original, "/opt/original")
        # Earlier stages' results: stage 3 reads stage 1's and stage 2's to decide
        # whether the rounds are worth running at all.  Both files, because both
        # rungs gate it -- a submission whose audit gate failed scores zero
        # whatever stage 2 measured, so its rounds would be paid for and discarded
        # exactly as an incomplete stage 2's are.  The glob is what makes
        # that work without a list here: it copies every result written so far, and
        # the loop above writes them in ladder order.
        for earlier in sorted(results.glob("*.json")):
            _copy_in(cid, earlier, f"/logs/verifier/{earlier.name}")

        _log(f"{stage.name}: {image} on network {_network_for(stage.name)}, "
             f"repo at {repo_in}, timeout {stage.timeout_sec:.0f}s")
        try:
            proc = subprocess.run(_docker("start", "-a", cid),
                                  timeout=stage.timeout_sec)
            code, note = proc.returncode, f"exit {proc.returncode}"
        except subprocess.TimeoutExpired:
            _run(_docker("kill", cid))
            code, note = 124, f"timed out after {stage.timeout_sec:.0f}s"
        # Collected whether or not the stage exited zero: a stage that failed
        # wrote a result saying so, and that result is the evidence.
        _copy_out(cid, "/logs/verifier", results)
        return code, note
    finally:
        _run(_docker("rm", "-f", cid))


def _find_original(task_dir: Path, scratch: Path) -> Path | None:
    """State A, unpacked, or None with a warning saying what that costs.

    Stage 1 compares the submission against State A, and its scan is written to
    fall back when the tree is absent -- so a missing State A does not fail, it
    quietly grades a weaker comparison.  That is the mistake worth being loud
    about rather than the one worth crashing on.
    """
    tarball = task_dir / "environment" / "original.tar.gz"
    if not tarball.exists():
        _log(f"warning: no State A at {tarball}, so stage 1 reviews the "
             f"submission against an empty tree and stage 3 loses its "
             f"reference. Mount it via tests/docker-compose.yaml.")
        return None
    return unpack_original(tarball, scratch / "state-a")


def cmd_ladder(args) -> int:
    """``swerefactor ladder`` -- the whole ladder from inside a Harbor verifier."""
    task_dir = Path(args.task_dir).resolve()
    eval_path = task_dir / "tests" / "evaluation.toml"
    try:
        evaluation = config.Evaluation.load(eval_path)
    except (config.ConfigError, OSError) as exc:
        harbor.publish_error(task_dir.name, args.results or "/logs/verifier",
                             f"cannot read {eval_path}: {exc}")
        print(f"[ladder] error: {exc}", file=sys.stderr)
        return 2

    results = Path(args.results or evaluation.results_dir)
    results.mkdir(parents=True, exist_ok=True)
    repo = Path(args.repo)

    if not repo.is_dir():
        harbor.publish_error(evaluation.task, results,
                             f"no submission at {repo}")
        return _fail(f"no submission at {repo}")

    probe = _run(_docker("version", "--format", "{{.Server.Version}}"))
    if probe.returncode != 0:
        # No socket means no stages, and the ladder cannot be faked from here.
        # Said as a harness error rather than a zero, because a zero would read
        # as a judgement about the submission.
        harbor.publish_error(
            evaluation.task, results,
            "no Docker daemon reachable from the verifier, so the three stage "
            "images cannot be run. Mount a socket into the verifier -- see "
            "tests/docker-compose.yaml -- or run the stages by hand and score "
            "with `swerefactor score`. docker said: "
            + (probe.stderr.strip() or "nothing"))
        return _fail("no Docker daemon reachable from the verifier")
    _log(f"docker daemon {probe.stdout.strip()}")

    # Before State A is unpacked and before any stage container starts: the two
    # model-driven stages cannot do anything without a key, and finding that out
    # here costs a second instead of a stage timeout.  A harness error, not a
    # zero -- publish_error writes valid=0, which says the grader did not
    # complete rather than that the submission failed.
    gap = _preflight_credentials(evaluation)
    if gap:
        harbor.publish_error(evaluation.task, results, gap)
        return _fail(gap)

    # Scratch, not results_dir: State A is thousands of files for the larger
    # tasks, and everything under results_dir is collected and published as the
    # verifier's artifacts.  A copy of the upstream release does not belong in
    # the evidence about a submission.
    scratch = Path(args.scratch or "/tmp/swerefactor-ladder")
    scratch.mkdir(parents=True, exist_ok=True)
    original = _find_original(task_dir, scratch)

    for name in ("audit", "behavioural", "verification"):
        stage = evaluation.stages.get(name)
        if stage is None or not stage.enabled:
            _log(f"{name}: not declared or disabled, skipped")
            continue
        try:
            code, note = run_stage(stage, eval_path=eval_path, repo=repo,
                                   original=original, results=results)
        except (OSError, RuntimeError) as exc:
            code, note = 1, f"{type(exc).__name__}: {exc}"
        _log(f"{name}: {note}")
        if code != 0 and not evaluation.result_path(name).exists():
            _log(f"{name}: left no result at {evaluation.result_path(name)}")
            _record_stage_failure(evaluation, name, note)

    # The ladder itself, on whatever the stages left behind: this is the same
    # entry point a task's score.py calls, so a run driven from here and a run
    # driven by hand are scored by one implementation rather than two.
    _log("scoring")
    return harbor.main(["--task-dir", str(task_dir), "--results", str(results)])


def _record_stage_failure(evaluation: config.Evaluation, name: str,
                          note: str) -> None:
    """Write the stage result a stage that produced none would have written.

    A stage can fail before it writes anything -- the copy-in pipe breaks, the
    image is absent, the whole stage overruns its own timeout -- and until this
    existed the only record was the ``[ladder]`` line above.  The scorer reads
    result *files*, so an absent one arrived as ``behavioural=None``, which
    ``grade_behavioural`` reports as "produced no result file" and the report then
    renders as ``NOT RUN (the audit gate failed)``: the driver's own reason
    discarded and replaced by whatever ``blocked_by`` happened to hold.

    Measured, not hypothetical.  Twelve runs in the 0807 campaign carry an empty
    stage-2 measurement, and the cause was invisible in every published verdict.
    All twelve published the same sentence blaming the audit gate, which had
    indeed failed -- so the false cause was consistent with the verdict and nothing
    contradicted it.

    ``status="error"`` rather than an empty ``ok`` result, because the two are read
    differently and only one of them is true: an ``ok`` stage with no modules grades
    as ``behavioural-empty`` -- an authoring fault, the task declaring no modules --
    while ``error`` carries ``harness_error`` and asks for a re-run, which is what a
    broken pipe deserves.  Neither publishes a zero against the submission.

    Written only when the stage left nothing.  A stage that wrote its own result has
    said something more specific than this can, and overwriting it would replace a
    measurement with a driver's guess at one.  The caller checks that too, and this
    checks it again: the caller's check is about whether to log, this one is the rule,
    and a stage list that grows a fourth entry will be edited by someone reading the
    loop rather than this.
    """
    path = evaluation.result_path(name)
    if path.exists():
        # The stage said something for itself.  Whatever it said outranks a note
        # composed from an exit code out here.
        return
    try:
        res = StageResult(stage=name, task=evaluation.task, status="error")
        res.notes.append(f"the {name} stage left no result: {note}")
        # Where the driver's note goes for a consumer that reads keys rather than
        # prose.  `grade_*` prefers `metadata["error"]` over the notes when it
        # composes `harness_error`, so this is the field that reaches the Harbor row.
        res.metadata["error"] = note
        res.metadata["recorded_by"] = "ladder"
        res.write(path)
        _log(f"{name}: recorded the failure at {path}")
    except OSError as exc:
        # Reported, never raised.  This runs on the path where something has already
        # gone wrong, and a driver that dies while writing a note about a stage
        # failure replaces a scorable run with a traceback.
        _log(f"{name}: could not record the failure at {path}: {exc}")


def _fail(message: str) -> int:
    print(f"[ladder] error: {message}", file=sys.stderr, flush=True)
    return 1


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="swerefactor ladder",
        description="run all three stage images as siblings, then score")
    parser.add_argument("--task-dir", default="/",
                        help="the task directory (holds tests/evaluation.toml)")
    parser.add_argument("--results", default="",
                        help="override results_dir from evaluation.toml")
    parser.add_argument("--repo", default="/workspace/repo",
                        help="the submission under test")
    parser.add_argument("--scratch", default="",
                        help="scratch directory for the unpacked State A "
                             "(default /tmp/swerefactor-ladder)")
    return cmd_ladder(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
