# Vendored inputs

Two tarballs and one directory of TypeScript. Everything here is read at **build
time** and none of it is in the grading image: the final stage deletes this whole
directory, which is the one place State A would otherwise reach it.

## `jsonata-2.2.2-test-suite.tar.gz`

The upstream project's own test suite, taken from the `test/test-suite/` directory
of jsonata 2.2.2: 102 groups, 1,686 cases, 28 datasets.

    sha256  9e323e85e3145bec70a32b4e58bf4af132042c58b111c18fcae432efc896ec3c

It is **vendored, not downloaded**. `lib/gen.py` reads it from this directory and
refuses to run without it, checking the digest above first. Nothing in the build
reaches the network: a grader that fetched its own inputs would grade a different
corpus on a day the network was slow, and a corpus that can change is not frozen.

The digest is checked rather than trusted: these cases carry 5.5% of the stage.
If the tarball is ever replaced deliberately, three things move together — the
digest, the `UPSTREAM_GROUPS`/`UPSTREAM_CASES`/`UPSTREAM_DATASETS` constants in
`lib/gen.py`, and the frozen corpus, which has to be regenerated because its case
ids are positional.

**The expected results in it are discarded.** Only the inputs are used — the
expression, the dataset, the bindings — and the answer is whatever the reference
implementation says when asked. Upstream writes its expectations in JSON, which
cannot spell four of the values that matter most here (`-0`, the two infinities,
`nan`); and a case whose upstream expectation has drifted from upstream's own
behaviour would otherwise be graded against a claim no implementation satisfies,
including the one being ported. Two categories are dropped whole, `timelimit` and
`depth`, because both measure the machine and its load rather than the port.

## `original.tar.gz` — here, and deleted from the image

State A, byte-identical to `environment/original.tar.gz`, with `original.sha256`
beside it. It is here because the `baseline` stage needs it: `COPY
data/original.tar.gz data/original.sha256 /opt/baseline/`, digest checked, unpacked,
built.

Two earlier drafts of this section said it was *not* here and *not* copied into the
behavioural image. Both were wrong, and the second one mattered. The final stage does
`COPY . /tests/behavioural`, this directory is in that context, and so the reference
shipped into the grading image at `/tests/behavioural/data/original.tar.gz` — mode
664, in the image where a submission is built and run. A `dist/probe.js` that
unpacked it and forwarded every request to the JavaScript would have answered all
13,940 cases and scored full marks as no port at all.

It is deleted now, by the `rm -rf /tests/behavioural/data` in the final stage, and
the deletion is checked two ways: the RUN fails if the tarball is not there to be
deleted, and `lib/check-archives.py` afterwards fails if any archive anywhere in the
image holds a `.js` or `.ts`. That second one is the check the image was missing.
Four name sweeps and one content sweep all looked at loose `.js`/`.ts` files, and a
gzipped tarball is invisible to every one of them.

The corpus itself is frozen at **image build time**: the reference is unpacked in an
earlier stage, every case is put to it, its answers become
`/opt/assets/corpus/*-expected.jsonl`, and the reference tree is gone before the
final stage. What ships is the questions and the answers, not the thing that
answered them.

That absence is the point. `node` is both the old runtime and the new one, so it
cannot be withheld from the grading image the way a stage can withhold a compiler
for a language nothing else needs — and a reference left in the image, as source or
as a tarball, is a working JavaScript implementation sitting next to a suite asking
whether a TypeScript one behaves the same way.

## `consumers/` — the type-surface fixtures

25 files: 24 TypeScript programs named by `catalog.TYPE_CASES`, plus the
`tsconfig.json` they compile under. They are read-only inputs, which is why they are
in `data/` rather than a directory of their own — `swerefactor validate` allows a stage
`data/`, `lib/` and `modules/` and nothing else.

They are *consumers*: each one imports the submission's declarations and either must
compile (positive rows) or must fail on one marked line (negative rows). Nothing in
them implements anything.

The oracle stage copies them to `/opt/assets/consumers` alongside the frozen corpus,
and the final stage reads them only from there. The copy here is deleted with the
rest of `data/`, so the two cannot drift into a state where a fix lands in the one
nothing reads — `lib/types-surface.py` falls back to this directory when
`SWEREFACTOR_ASSETS` is unset, which is exactly when a stale copy would be used
silently.

## `/opt/assets/corpus/` — written at build time, not in this directory

Eight files, four stems (`main`, `fresh`, `protocol`, `fixture`), each a
`-cases.jsonl` and a `-expected.jsonl`. Absent from the source tree by design;
`lib/check-assets.py` accepts a tree with none and rejects a tree with half of one.

Cases pair with expectations **by position, not by id**. The protocol stem has 117
cases that all report id 0, so a reader keyed by id collapses them onto one entry —
which cost an earlier task 11 of its 33 protocol cases, silently, and is why
`lib/freeze.py` is the only reader of the format.
