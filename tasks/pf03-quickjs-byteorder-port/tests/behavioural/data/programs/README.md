# The programs

Each `.js` file here is run by every one of this stage's six builds, and prints
lines of the form

    key: value

to stdout. Nothing else. That is the whole format.

It is also the format stage 3 asks its candidates for, so an adversary constructs
the same kind of artifact this stage grades and the two cannot drift into measuring
different things. A candidate's program is run on all three of a tree's targets and
compared the same way.

## Keys

A key names one observation. `dv-get-u32-le`, not `test1`. The name goes into the
report verbatim, so a reader who has never seen the program can tell what diverged.

**A key beginning `nat-` is native-order dependent**, declared so by whoever wrote
the program. Those are the observations a big-endian target is *supposed* to answer
differently: the bytes a `Float64Array` shows through a `Uint8Array` aliasing the
same buffer, `DataView` with no endianness argument, `TypedArray.prototype.toString`
over a reinterpreted buffer.

Nothing compares a `nat-` key across targets. `native-order` grades them as an
internal-consistency question instead — whatever order a target uses, every width
and every API must agree on it, and it must be the order that target's ABI
specifies. Comparing them against x86-64 would fail every correct submission, and
pass one that byte-swapped its typed arrays into agreement. That second thing is a
real bug: upstream's own `tests/test_builtin.js` has the little-endian answer
hardcoded at line 442, so a correct big-endian build fails it, and "made the test
pass" is the wrong repair.

Every other key is a candidate invariant, and the `build` module decides. A key is
admitted only if reference-x86-64 and reference-armhf printed the same value for it
in this run: those two differ in word size and agree in byte order, so a key they
disagree on was never invariant and grading against it would be the grader's
arithmetic rather than the submission's defect. Excluded keys are reported and
unscored.

## Interpreter flags

A program may ask for them on line 1:

    // qjs-args: --bignum

Read from this file, never from the submission.

## What a program should be

Deterministic. No clock, no locale, no filesystem, no network, no thread ordering.
Every value here has to be one that a correct interpreter answers identically on
two runs of the same binary, or the divergence it reports is noise. `Date` appears
in `dates.js` only through `Date.UTC` and explicit epoch values, and the harness
pins `TZ=UTC`.

Small. One program that prints ninety keys is one timeout away from losing all
ninety; the failure of a program is reported per program, and a narrow one keeps its
neighbours' evidence.

Honest about what it is testing. `dataview-explicit.js` passes an explicit
`littleEndian` argument to every accessor, so its answers are spec-fixed on every
machine ECMAScript runs on. That is why its keys carry weight. A program that prints
the raw bytes of a buffer is testing the ABI, and its keys are `nat-`.
