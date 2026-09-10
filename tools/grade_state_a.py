#!/usr/bin/env python3
"""Grade State A against its own stage 2, where it should pass everything.

Stage 2 compares a submission with a recording taken from State A, so State A
submitted unchanged is the one input whose answers are known in advance: every
check it fails is a check no submission can pass.  That makes this the cheapest
guard there is against a recording drifting away from the suite that reads it --
it runs offline, needs no model and no key, and the only thing it costs is the
stage image.

The images are not built here; build the ones you want to measure first:

    docker build -t swerefactor/infra:1 infra/
    docker build -t swerefactor/fw01-behavioural:1 \\
        tasks/fw01-httpbin-flask-to-asgi/tests/behavioural

    PYTHONPATH=infra tools/grade_state_a.py [--task NAME ...]

Two tasks are outside its reach and are skipped unless named explicitly: lang02
and lang06 drive an artifact of the target platform, which State A does not
build, so their suites report "not run" rather than a comparison.

Exit status is 0 when every task measured came back clean, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

from swerefactor import config, ladder

REPO = Path(__file__).resolve().parents[1]
TASKS = REPO / "tasks"

#: State A is not a runnable submission for these; see the module docstring.
NOT_RUNNABLE = ("lang02-zlib-c-to-java", "lang06-jsonnet-cpp-to-csharp")


def unpack(task: Path, into: Path) -> Path:
    """State A, laid out the way a submission would be."""
    raw = into / "unpacked"
    with tarfile.open(task / "environment" / "original.tar.gz") as archive:
        archive.extractall(raw)
    inner = raw / "repo"
    source = inner if inner.is_dir() else raw
    repo = into / "repo"
    repo.mkdir()
    for item in source.iterdir():
        if item.is_dir():
            shutil.copytree(item, repo / item.name, symlinks=True)
        else:
            shutil.copy2(item, repo / item.name, follow_symlinks=False)
    return repo


def failures(result: Path) -> tuple[int, int, dict[str, int]]:
    checks = json.loads(result.read_text(encoding="utf-8")).get("checks") or []
    by_unit: dict[str, int] = {}
    for check in checks:
        if check.get("verdict") != "pass":
            unit = check.get("unit", "?")
            by_unit[unit] = by_unit.get(unit, 0) + 1
    return sum(by_unit.values()), len(checks), by_unit


def grade(task: Path) -> bool:
    eval_path = task / "tests" / "evaluation.toml"
    evaluation = config.Evaluation.load(eval_path)
    stage = evaluation.stages.get("behavioural")
    if stage is None:
        print(f"{task.name}: no behavioural stage")
        return True
    with tempfile.TemporaryDirectory(prefix="state-a-") as tmp:
        work = Path(tmp)
        results = work / "results"
        results.mkdir()
        repo = unpack(task, work)
        status, note = ladder.run_stage(stage, eval_path=eval_path, repo=repo,
                                        original=repo, results=results)
        result = results / "behavioural.json"
        if not result.is_file():
            print(f"{task.name}: the stage wrote no result ({status}: {note})")
            return False
        failed, total, by_unit = failures(result)
        print(f"{task.name}: {failed} failing of {total} checks"
              + (f" -- {by_unit}" if by_unit else ""))
        return failed == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", action="append", default=[],
                        help="task directory name; repeatable, default all")
    args = parser.parse_args(argv)

    names = args.task or [p.name for p in sorted(TASKS.iterdir())
                          if p.is_dir() and p.name not in NOT_RUNNABLE]
    clean = True
    for name in names:
        task = TASKS / name
        if not task.is_dir():
            print(f"{name}: no such task")
            return 1
        clean &= grade(task)
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
