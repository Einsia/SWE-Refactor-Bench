#!/usr/bin/env python3
"""Score fw07: apply the published ladder to whichever stages ran.

    stage 1  audit     fail  -> 0, and stage 3 is not run.  Stage 2 is still
                           run and reported; by policy it earns nothing
    stage 2  behavioural    40 points, all or nothing: one failed check and the
                           ladder stops here, with `swerefactor verification`
                           declining to run the rounds
    stage 3  verification   60 points, 10 for each of 6 models that found nothing

The ladder itself is in ``swerefactor.harbor``, not here.  It is the benchmark's
policy rather than this task's, and private copies of it would be things to keep in
step -- a task that scored differently from the published policy would be a task
nobody could compare against the others.  What lives here is the entry point, and
the numbers that make fw07 fw07 are in ``tests/evaluation.toml`` where a reader can
see them.

Reads, from ``results_dir``:

    audit.json     written by `swerefactor audit`     (stage 1's image)
    behavioural.json    written by `swerefactor behavioural`    (stage 2's image)
    verification.json   written by `swerefactor verification`   (stage 3's image)

Writes, to the same place:

    reward.json        numerics only -- what Harbor reads
    score.json         the whole verdict
    summary.txt        the readable report

A stage file that is absent is not a stage that failed: stage 3 has no file when
the ladder correctly stopped at stage 2, and the scorer tells those apart by which
stage stopped it rather than by which files exist.

The one thing this file adds to the shared wrapper is a fault report, and the
reason is specific to stage 3.  See ``_report_faults``.
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

#: Where stage 3's driver leaves a note when it could not run a candidate at all.
#: ``$SRB_WORK`` is set per-execution by CandidateRunner to ``<work>/run``; the
#: second entry matches `swerefactor verification`'s own default, for the case where
#: this scorer runs in a shell that never had the variable.
FAULT_DIRS = (
    Path(os.environ.get("SRB_WORK", "/tmp/swerefactor-verification/run")) / "faults",
    Path("/tmp/swerefactor-verification/run/faults"),
)


def _report_faults() -> None:
    """Print stage 3's infrastructure faults above the verdict, if any.

    Printed, and deliberately not scored.  Stage 3 judges a candidate by its exit
    status, and `discriminates` is `original passed and submission did not` -- so a
    tree that could not be BUILT or BOOTED on the submission side is
    indistinguishable from a real divergence, and a persistent one reproduces
    across all three reruns and is upheld.  Ten points come off a submission for a
    defect it does not have.

    On this task that hazard is larger than on a task whose subject is one binary,
    because there is more between the tree and a request being answered: an offline
    Maven build of an eleven-module reactor, then a launch, then an OSM import with
    contraction-hierarchy and landmark preparations, twice per tree.  Any of those
    can fail for a reason that is the image's or the machine's rather than the
    submission's -- a heap the machine could not give, a port that was still held,
    a disk that filled with two graph caches -- and each failure looks from the
    adjudicator's seat exactly like a rewrite that broke something.

    The tempting fix -- treat a fault as a failure on both sides -- inverts the
    error rather than removing it: no claim can then be upheld, every round records
    a survival, and the submission collects all 60 points without being attacked.
    Both directions are wrong, and which one is right depends on why the tree would
    not build, which is exactly the thing a scorer cannot see.

    So the fault is surfaced instead, with the tree token, the exit code and the
    reason, immediately above the numbers it might have distorted.  A reviewer can
    then read the round transcript and decide.  The tokens are the harness's
    per-run hashes rather than "original" and "submission", which is deliberate --
    stage 3 does not write down which tree is which -- but the two tokens are
    distinct and stable within a run, so faults concentrated under one token say
    "one side was broken all stage" while faults under both say "the image or the
    machine was".

    In a healthy run this prints nothing at all, which is the case that matters: a
    warning that appears on every run is a warning nobody reads.
    """
    seen: set[Path] = set()
    notes: list[str] = []
    for base in FAULT_DIRS:
        base = base.resolve()
        if base in seen or not base.is_dir():
            continue
        seen.add(base)
        for note in sorted(base.iterdir()):
            try:
                notes.extend(line.rstrip("\n") for line in
                             note.read_text(encoding="utf-8").splitlines()
                             if line.strip())
            except OSError:
                notes.append(f"(a fault note at {note} could not be read)")
    if not notes:
        return
    bar = "=" * 78
    print(bar, file=sys.stderr)
    print(f"STAGE 3 INFRASTRUCTURE FAULTS: {len(notes)}. Read these before the "
          f"score.", file=sys.stderr)
    print("A tree that could not be built or booted reads to the adjudicator "
          "exactly", file=sys.stderr)
    print("like a candidate that found a defect. Each line below is a candidate "
          "run that", file=sys.stderr)
    print("never happened -- tree token, exit code, reason:", file=sys.stderr)
    for note in notes:
        print(f"  {note}", file=sys.stderr)
    print("  (71 = the tree would not build, 74 = it built but never bound a "
          "port,", file=sys.stderr)
    print("   70 = the stage-3 driver itself failed)", file=sys.stderr)
    print("The verification points below may be wrong in either direction. The "
          "round", file=sys.stderr)
    print("transcripts under the stage's log directory say which.", file=sys.stderr)
    print(bar, file=sys.stderr)


if __name__ == "__main__":
    argv = sys.argv[1:]
    if not any(a.startswith("--task-dir") for a in argv):
        argv += ["--task-dir", str(HERE)]
    _report_faults()
    raise SystemExit(harbor.main(argv))
