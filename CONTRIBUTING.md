# Contributing

Two kinds of change land here, and they have different bars: the shared harness
under `infra/`, and a task under `tasks/`. A harness change is reviewed like
ordinary code. A task is a measurement instrument, and the bar is that it has been
shown to measure what it claims.

## Before anything else

```bash
python3 -m pytest infra/tests -q                       # the harness's own suite
PYTHONPATH=infra python3 -m swerefactor validate --task-dir tasks/<id>
```

The suite finds the harness through `pyproject.toml`'s `pythonpath`; `-m
swerefactor` does not, so it needs `PYTHONPATH=infra`. This is the same path the
sixty stage images take — they set `PYTHONPATH=/opt/swerefactor` and copy the source
in rather than installing it; `pytest` is the only thing to install.

Both run offline on Python 3.10+ with no dependency but `tomli` below 3.11.
Neither builds an image. `validate` is what
catches an authoring mistake before an image is ever built — a gate declared but
absent from the prompt, weights that do not normalise, a module whose command does
not exist, a recorded tarball digest that has drifted from the file beside it.

## Changing the harness

`infra/swerefactor/` is shared by sixty stage images and twenty scorers, so a change
here changes every task at once. Three things that are easy to get wrong:

**A test must be able to fail.** The suite is written so each test names the
mistake it would catch, and several were added after a passing test turned out to
assert nothing. If you add a check, break the thing it checks and watch it go red
before you commit. A test whose assertion holds over an empty list is not a test.

**Provenance is stamped, never recomputed.** `StageResult.harness` records which
harness graded a result. `from_dict` copies whatever was written, empty included:
a result read off disk and written back out must not claim the current build
graded it. The same goes for `Verdict.harnesses`.

**"The stage crashed" and "the submission failed" are separate axes.** `status` is
about the run, `verdict` is about the submission, and a scorer that conflates them
turns an outage into a zero. If you touch scoring, `docs/SCHEMA.md` is the
contract and `infra/tests/test_scoring.py` is where the edge cases are written
down.

## Adding or changing a task

A task is `tasks/<category><nn>-<slug>/`, and the shape is described in
`docs/SCHEMA.md`. What is not obvious from the shape:

**State A must be a real upstream release, frozen.** It ships verbatim as
`environment/original.tar.gz`, built with `tools/build_repo_snapshot.py`. That
tool prints the new digest and updates nothing, so every place the digest is
recorded — between two and thirteen files per task, in four different spellings —
is re-pinned by hand. `swerefactor validate` cross-checks all of them against the
tarball, which is the only thing standing between a re-pin and a stage that
verifies a digest no longer present.

**Both directions have to be measured, not argued.** A task is not done until it
has been run against a known-good submission (expect a high behavioural score) and
against untouched State A (expect stage 1 to zero it). Record both in the commit
that adds the task, with the command that produced them, so the claim travels
with the tree it was measured against.

**A cheat must be stopped by the gate that is supposed to stop it.** Deliberate
cheats are part of a task's validation, and each must be caught by its intended
gate. A cheat caught by accident is a cheat that a slightly better cheat gets
past, so a validation run that says "blocked" without saying "by which gate" has
not established anything.

**State A must reach stage 2's full marks.** Stage 2 asks one question — does the
observable behaviour match State A's — so the frozen release is the answer to that
question and scores full marks by construction; whether a tree was migrated at all
is stage 1's question, and stage 1 is a gate. That makes State A the suite's
calibration: stage 3 runs only when stage 2 is perfect, so a behavioural suite the
release it was written from cannot clear puts the 60 verification points out of
reach for a reason no submission can fix. Measure it once when you add the task,
and name the run that showed it.

**Quote the ladder's numbers in `instruction.md` or don't, but don't drift.** The
instruction is the only graded artifact an agent reads and the only one no code
consumes. `validate` holds four sentence shapes against `[scoring]` — a stage's
share of the whole, the full-marks entry condition, the adversary count, and a
per-adversary figure multiplied out — so if you retune a task's ladder, run
`validate` and let it tell you which prose followed. Fourteen tasks state no
figures at all, which is also fine; the check reports that rather than passing
silently.

**Vendored terms travel with the code.** Each release stays under its own licence,
with its full text inside its own archive, and `NOTICE` states them per task. If
you add a task, add its entry, and note any file whose terms differ from its
project's own. `python3 tools/verify_upstream_licences.py` re-checks each licence
file against upstream at the recorded ref; it needs network, so it is run by hand
rather than in CI.

## Documentation

`docs/SCHEMA.md` is the contract; if you change a file format, change it in the
same commit. Anything you write states its method inline — a number quoted from a
run directory has to be reproducible from the text around it, because `/_run/` and
`/_work/` are gitignored and the reader does not have them.

## Commits

One commit per graded artifact. Splitting a change to a task's configuration by
hunk leaves commits that describe a tree no one can grade, and the point of the
history here is that any commit can be checked out and measured.

Say what the change makes true, not what you did: "Tell a tree that could not be
tested apart from one that behaved differently" over "fix stage 3 bug". The
existing log is the style guide.
