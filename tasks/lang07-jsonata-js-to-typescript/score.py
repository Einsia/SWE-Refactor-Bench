#!/usr/bin/env python3
"""Score lang07: apply the published ladder to whichever stages ran.

    stage 1  audit     fail  -> 0, and stage 3 is not run.  Stage 2 is still
                           run and reported; by policy it earns nothing
    stage 2  behavioural    40 points, all or nothing: one failed check and the
                           ladder stops here, with `swerefactor verification`
                           declining to run the rounds
    stage 3  verification   60 points, 10 for each of 6 models that found nothing

The ladder itself is in ``swerefactor.harbor``, not here.  It is the benchmark's
policy rather than this task's, and a private copy of it would be a thing to keep
in step -- a task that scored differently from the published policy would be a task
nobody could compare against the others.  What lives here is the entry point, and
the numbers that make lang07 lang07 are in ``tests/evaluation.toml`` where a reader
can see them.

Stage 2's 40 points are paid whole or not at all, so the fifteen modules and the
2,349 weight spread across fourteen of them set the rate the report publishes
rather than a price.  ``build`` carries no weight because it is the step that
produces the artefact the other fourteen measure, and a tree that does not
build fails their checks without needing a veto of its own.
``tests/behavioural/suite.toml`` holds the split and
``tests/behavioural/lib/catalog.py`` holds the per-family budgets it is checked
against.

One thing about this task's ladder is worth knowing before reading a report from
it, because it looks like a weighting mistake and is not.  For this language pair
stage 1 is not a formality that precedes the real measurement -- it *is* the
measurement of whether the rewrite happened.  Renaming the nine ``src/*.js`` to
``.ts``, adding ``// @ts-nocheck`` to each and compiling under a strict-looking
tsconfig passes every scored check and collects all 40; a control that changes one
line of behaviour misses a check and collects nothing.  Behaviourally a rename is
the original, so no behavioural module can separate them, none is asked to, and
stage 2 can order the two of them backwards.  A submission that reaches stage 3 has
already been read by three independent reviewers against the original, and a run
that shows 0.0 with a full-marks behavioural stage attached is the ladder working
rather than disagreeing with itself.

Reads, from ``results_dir``:

    audit.json     written by `swerefactor audit`    (stage 1's image)
    behavioural.json    written by `swerefactor behavioural`   (stage 2's image)
    verification.json   written by `swerefactor verification`  (stage 3's image)

Writes, to the same place:

    reward.json        numerics only -- what Harbor reads
    score.json         the whole verdict
    summary.txt        the readable report

A stage file that is absent is not a stage that failed: stage 3 has no file when the
ladder correctly stopped at stage 2, and the scorer tells those apart by which stage
stopped it rather than by which files exist.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The harness is on PYTHONPATH inside every stage image (/opt/swerefactor).  Outside
# one -- someone re-scoring a finished run from a checkout -- fall back to the
# repository's own infra/ so this file works in both places.
if "swerefactor" not in sys.modules:
    for candidate in (Path(os.environ.get("SRB_INFRA", "/opt/swerefactor")),
                      HERE.parent.parent / "infra"):
        if (candidate / "swerefactor" / "__init__.py").exists():
            sys.path.insert(0, str(candidate))
            break

try:
    from swerefactor import harbor
except ImportError as exc:  # pragma: no cover - environment problem, not logic
    print(f"cannot import the swerefactor harness: {exc}", file=sys.stderr)
    print("expected it on PYTHONPATH, at $SRB_INFRA, or in infra/ of the "
          "repository", file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    argv = sys.argv[1:]
    if not any(a.startswith("--task-dir") for a in argv):
        argv += ["--task-dir", str(HERE)]
    raise SystemExit(harbor.main(argv))
