"""Shared grading machinery for SWERefactorBench.

The three stages, the one result contract they all write, and the single scorer
that reads it.  A task supplies its prompts, its module manifest and its weights;
none of the logic below lives in a task directory, because twenty copies of a
scoring rule is twenty chances for them to disagree.

    result.py       the stage-result contract: Check, Unit, StageResult
    config.py       readers for evaluation.toml, suite.toml, probe.toml
    scoring.py      the ladder: gate -> behavioural 40 -> verification 60
    report.py       the human-readable rendering of a verdict

    tools.py        filesystem access given to a reviewing model
    models.py       model drivers (anthropic, openai, command, scripted)
    agentloop.py    the conversation cycle both model stages share

    audit.py     stage 1, the audit review
    behavioural.py   stage 2, the module runner
    verification.py  stage 3, the six rounds
    pytest_module.py  plugin letting an existing pytest suite be a module
    telemetry.py    what a run's grading models cost in time and tokens

Submodules are not imported here.  Grading a behavioural result should not drag in
urllib and subprocess, and a task's ``score.py`` imports two names.

See ``docs/SCHEMA.md`` for the file formats.
"""


def fingerprint() -> str:
    """A digest over the harness's own source, as 12 hex characters.

    A name alone cannot attribute a score to a harness: the sixty stage images
    take their copy from a mutable tag (``ARG INFRA_IMAGE=swerefactor/infra:1``), so
    a rebuilt donor image changes what graded a submission without anything else
    in the record moving.  A digest over the source moves whenever the source
    moves, which is the property a reader needs when two runs of the same task
    disagree.

    Computed from the ``.py`` files beside this one, sorted by name, each mixed in
    with its name so that moving code between modules registers.  Nothing here
    reads the filesystem outside the package, and a source file that cannot be
    read is recorded as unreadable rather than skipped: a fingerprint that
    silently omits a module is a fingerprint that claims two different harnesses
    are the same one.
    """
    import hashlib
    from pathlib import Path

    h = hashlib.sha256()
    for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
        h.update(path.name.encode())
        try:
            h.update(path.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
    return h.hexdigest()[:12]
