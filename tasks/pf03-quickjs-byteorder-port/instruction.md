# Port QuickJS to a big-endian target

`/workspace/repo` is QuickJS 2020-11-08, unmodified upstream: a complete
JavaScript engine in about 85k lines of C — bytecode compiler, interpreter, a
serialiser for compiled code and data, an arbitrary-precision numeric layer, a
REPL, and `qjsc`, which turns a `.js` file into C source with a bytecode blob
embedded in it.

The engine works on little-endian machines only, and that is not a build
configuration in this tree. `cutils.h:32` reads:

```c
/* set if CPU is big endian */
#undef WORDS_BIGENDIAN
```

Unconditionally, on every machine. Every big-endian path downstream of that macro
is dead code, on every target, and nothing tells you so: the tree compiles and
links for a big-endian target and produces a `qjs` binary. The binary is wrong,
and it is wrong quietly — it starts, it runs scripts, and it gives some wrong
answers.

Your job is to make this repository correct on a big-endian target without
changing what it does on the little-endian ones.

## 1. The three targets

One source tree has to build three ways. The toolchains and the emulators are
installed.

| target | word | order | role |
|---|---|---|---|
| `x86_64` | 64-bit | little-endian | the reference. Its answers are the contract. |
| `s390x` | 64-bit | **big-endian** | the port. This is the work. |
| `armhf` | 32-bit | little-endian | the control. |

`armhf` is there to separate two things that are easy to conflate. It changes
word size without changing byte order, so a change that is really about pointer
or limb width cannot hide inside a change that claims to be about endianness. It
already agrees with `x86_64` on everything measured, apart from answers that
follow from a 32-bit word. If your work moves `armhf`, you have broken something
that was working.

## 2. The invocation that is built

This is the command, and it is the whole build contract. Your tree must satisfy
it unchanged:

```
# x86_64
make CONFIG_LTO= -j$(nproc) \
     qjs qjsc libquickjs.a tests/bjson.so examples/point.so examples/fib.so \
     examples/hello examples/test_fib

# s390x
make CONFIG_LTO= -j$(nproc) CROSS_PREFIX=s390x-linux-gnu- \
     qjs qjsc libquickjs.a tests/bjson.so examples/point.so examples/fib.so \
     examples/hello examples/test_fib host-qjsc

# armhf
make CONFIG_LTO= -j$(nproc) CROSS_PREFIX=arm-linux-gnueabihf- \
     qjs qjsc libquickjs.a tests/bjson.so examples/point.so examples/fib.so \
     examples/hello examples/test_fib host-qjsc
```

Three things about it are worth reading twice.

`CROSS_PREFIX` is the only thing that distinguishes a cross build. `CC` is not
passed; upstream's `Makefile` derives the compiler from the prefix, and that is
the mechanism you are being asked to work with. Nothing sets a macro on the
command line to tell the tree what order the target is — deciding that is the
task.

`CONFIG_LTO=` clears LTO, because the cross toolchains here have no matching
linker plugin. It has nothing to do with byte order, and the native build is
invoked the same way, so all three targets are built alike.

`host-qjsc` is a *host* binary built from these same sources with the host
compiler. Upstream adds it to the goals only on a cross build and then points
every blob-emitting rule at it. It is the centre of §3.5.

Run the binaries like this:

```
./qjs                                                   # x86_64
qemu-s390x-static -L /usr/s390x-linux-gnu ./qjs          # s390x
qemu-arm-static -L /usr/arm-linux-gnueabihf ./qjs        # armhf
```

`examples/hello_module` is the one exception in this tree and it is outside the
task. It is built through `qjsc -o`, which shells out to a linker for the
architecture `qjsc` was configured with — the host's, on a cross build. That is a
cross-build defect of upstream's Makefile with nothing to do with byte order. It
is not built, not measured, and fixing it earns nothing.

## 3. What must be true when you are done

### 3.1 The tree derives its target's byte order

The same sources, handed to a different target's compiler, must reach a different
conclusion about byte order. How is yours to choose: the compiler's own
byte-order macros, a platform header and its constants, a build-system probe that
compiles or runs something. There is no preferred idiom and no spelling anything
looks for.

What is not acceptable is asserting the answer — defining the macro
unconditionally, keying the decision on a compiler or distribution name instead of
on byte order, computing a result the code that needs it does not use, or leaving
two detections that can disagree.

### 3.2 Typed-array storage stays in the target's own order

This is the item most likely to be got backwards, so it is stated first among the
behavioural ones.

A big-endian machine is *supposed* to show a `Float64Array`'s bytes reversed when
you read them through a `Uint8Array` over the same buffer. ECMAScript leaves the
storage order of a typed array to the platform, and the correct answer on `s390x`
is big-endian. `new Uint16Array([0x0102])` viewed as bytes begins with `0x01`
there and with `0x02` on the other two.

Do **not** byte-swap typed arrays into agreement with `x86_64`. A tree that makes
all three targets print the little-endian byte pattern has not ported the engine;
it has introduced a swap where the language does not call for one, and that is
measured as a defect rather than a fix.

What is required instead is that the order be *coherent*. Changing byte order
permutes bytes within a field and does nothing else: it does not move a field,
resize one, or reorder two of them. Every width and every interface over the same
buffer must agree about which order the machine uses — 16-, 32- and 64-bit
integers, `Float32` and `Float64`, `BigInt64`, and the aliasing between a typed
array and a `DataView` told an order. A tree that swapped 32-bit accesses and left
64-bit ones alone is internally inconsistent, and that is visible.

### 3.3 `DataView` with an explicit order means what it says

`setUint32(0, x, true)` writes little-endian and `setUint32(0, x, false)` writes
big-endian, on every target, and each getter reads back what the corresponding
setter wrote. This is the one API where a program names a byte order, and the
specification fixes every one of these answers. They must be identical on all
three targets, at every width and signedness, aligned and unaligned.

This is where State A is visibly broken: `s390x` answers a large fraction of these
wrongly today, and nowhere else in the engine's ordinary scripting produces a wrong
value.

Note how §3.2 and §3.3 compose rather than conflict. An explicit-order `DataView`
read over a typed array's buffer on `s390x` is a big-endian read of big-endian
storage, and its result is therefore a value the specification fixes — not a
native-order one.

### 3.4 Views over one buffer agree

Any two typed arrays or `DataView`s over the same `ArrayBuffer` see one another's
writes with one consistent interpretation, including reads through a view whose
element size differs from the writer's, and including `Atomics` on a
`SharedArrayBuffer`.

### 3.5 A blob the host wrote is read correctly by the target

This tree bootstraps itself, and on a cross build the bootstrap crosses a byte
order.

Two C files in the build — the REPL and the calculator — are not written by hand.
They are bytecode blobs produced by compiling JavaScript with this engine's own
compiler, and upstream's `Makefile` points those rules at `./host-qjsc`, which runs
on the **host**. `examples/hello` and `examples/test_fib` are built the same way:
the host's `qjsc` emits C, the target's compiler compiles it, and the target's
interpreter reads the blob inside it.

Every binary that carries a blob must run on its target and compute the right
answer. Today on `s390x` none of them do: the blob is in the host's order, carrying
a version tag the target's reader accepts, so the reader takes the body at face
value and reads every integer in it byte-reversed. What comes out is not a clean
rejection — it is a nonsense value used as if it were real, and what you see is an
error naming it, or a crash.

`qjs -e 'print(40+2)'` works, and so does running a script file. That is the tell:
the engine and its startup are fine, and the fault is in the path where a blob
compiled by one machine is read by another.

How you fix it is open. Making the writer emit the target's order and making the
reader accept either order are both legitimate, and they ship different bytes. The
encoding is not the contract — nothing measures the bytes of a blob, its header, or
its version tag. What is measured is whether the binary runs and computes the right
answer. The build has to *know* when the host and the target disagree; a swap
hardcoded on breaks the native build, which is the same mistake pointing the other
way.

### 3.6 The numeric serialiser crosses that boundary too

Fixing the build is not sufficient, and this is the part a plausible-looking port
misses.

Arbitrary-precision integers are serialised limb by limb, and the mantissa loop
does not go through the same put-integer helpers as the rest of the stream. It
writes limbs with the raw fixed-width writers, so a stream-level swap never reaches
them; the only thing that orders a limb is the compile-time decision of §3.1. A
`BigInt` literal in a source file is built while compiling and lands in the
bytecode, so it crosses the host-to-target boundary of §3.5 — where a tree that
fixed the transport and stopped returns a wrong value.

`qjs script.js` never reaches these paths. Reaching them takes a literal compiled
into a blob by one order and read back by another.

### 3.7 Everything the specification fixes is unchanged, on all three targets

The reference is the pristine tree built for `x86_64`. Every answer it gives —
number formatting, `toFixed`/`toPrecision`, radix conversion, `BigInt` and
`BigDecimal` arithmetic, string operations, `RegExp`, `JSON`, `Date`, sort
stability, iteration order the specification fixes, which exceptions are thrown —
must still be that answer on `x86_64`, and must now be that answer on `s390x` and
`armhf` too.

This cuts both ways, and the second way is the one that catches an over-reach. A
tree that reaches the byte-order sites by rewriting something broader — the parser,
the number formatter, the string comparator, an atom table, the hash of a property
key — moves answers here. A swap applied where the format does not call for one is
not a byte-order fix; it is a new bug on all three targets.

### 3.8 The feature set and the public interface do not move

`CONFIG_BIGNUM` stays on. `Atomics` and `SharedArrayBuffer` stay present.
`libquickjs.a` keeps exporting the same symbols with the same signatures, and
`quickjs.h` is a published header: a program that compiled against State A must
compile and link against what you leave.

Disabling a feature on the big-endian target because it was inconvenient there is
not a port. Neither is guarding a code path so it only runs on one target when the
language requires it on all of them.

### 3.9 Upstream's own tests

`tests/` ships suites the `Makefile` runs. They are run on all three targets, and
each one is compared against what that same suite does in the pristine tree on the
same target. A suite that fails in both trees is not charged to you.

That last sentence matters here, because of §3.2. `tests/test_builtin.js:442`
asserts a little-endian byte string:

```js
assert(a.toString(), "0,0,255,255,0,0,0,0,0,0,128,63,255,255,255,255");
```

On a big-endian machine a *correct* engine fails that assertion, because the
storage really is big-endian there. It fails in the pristine tree on `s390x` and it
will fail in yours. Leave it alone. It is an upstream test written on the
assumption this task exists to remove, and an upstream bug faithfully preserved is
a correct port.

You may add tests. What you may not do is edit an existing assertion so that it
passes — if a test's assertion looks wrong to you, it is either telling you what
the contract is or it is the case just described, and neither is fixed by changing
it.

## 4. Differences that belong to the platform, not to you

Three targets means some answers legitimately differ, and being charged for those
would make the task unwinnable. The measurement is built to separate them, and it
helps to know how, because it tells you which differences to leave alone.

The expectation for a cross-target answer is taken from the pristine tree on
`x86_64` — but only for answers the pristine tree gives *identically* on `x86_64`
and `armhf`. Those two are both little-endian and differ in word size, so an answer
that moves between them was never a byte-order invariant: it depends on pointer
width, or on `long`, or on the C library, and this task does not change any of
those. Such answers are reported and not scored.

That leaves the differences you are responsible for as exactly the ones byte order
causes. Concretely:

- **Word size.** `armhf` is 32-bit. Sizes, addresses, and anything derived from
  them are its business, not yours.
- **The C library.** Where the language leaves a result implementation-defined —
  the *magnitude* of a comparison, the text of a system error, locale behaviour —
  glibc may answer differently per architecture in a perfectly correct build. Do
  not chase it.
- **Native order itself.** The handful of answers whose correct value *is* the
  machine's own byte order have no cross-target expectation, by construction. They
  are checked for agreeing with the machine they ran on. See §3.2.
- **Upstream's little-endian assumptions.** §3.9.

If you find yourself writing a special case whose only purpose is to make `s390x`
or `armhf` print what `x86_64` prints, stop and ask which of the two it is: a byte
order that should have been handled at the format boundary, or a difference that was
never yours. The first is the task. The second is a bug you are about to add.

## 5. How the work is judged

Three stages, and each looks at something different.

**The tree is read.** Your repository and the pristine one are handed to a reviewer
that can list, read, search, and diff, and cannot build or run anything. It is asked
one question — is this a byte-order port of this engine — and it answers it by
reading your diff against §3 and pointing at the file and line it is talking about.
It is not matching patterns, so there is no spelling to adopt and none to avoid;
what persuades it is a change that makes sense as the port §3 describes. This stage
is pass-or-stop: a tree that fails it is not built.

**The tree is built and run.** Both trees are compiled for all three targets and the
binaries are exercised — the same programs, the same inputs, against both. What is
compared is what the programs print and what the binaries do. Nothing at this stage
reads your source. The third stage happens only if this one comes out at full marks.

Be precise about what this stage charges you for, because it is not "how much of the
port is done". Every value it grades against was recorded from the pristine tree, and
on the big-endian target the pristine tree is wrong — that is the premise of the
task. So a value the original does not have is not charged to anyone here. What is
charged is: everything the original got right and you changed, on all three targets;
and every value you produce through an operation the original could not complete but
you got working. The second half is the one to notice. Getting the bytecode
serialiser far enough to round-trip a value and then handing back the wrong bytes
costs more than never getting it running at all, and that is deliberate — the first
stage is where "did the port happen" is settled, and it is pass-or-stop.

Read that together: do not break what works, and do not half-finish what you start.

**The tree is attacked.** Independent adversaries get both repositories and both
sets of built binaries. Each reads your diff, forms a theory about where the port is
thin, and writes an input meant to make your binaries and the reference's disagree —
same shape of input as the second stage, in a program whose answer the language
pins down. Every adversary that fails to find one is a point in your favour.

Two consequences worth internalising. The second and third stages never look at your
source, so no amount of code shaped to look ported helps there; only behaviour does.
And the third stage is verification and open-ended, so the parts of §3 that are hard
to reach from a shell script — §3.6 especially — are where it will go. "The suites
pass" is not the standard. "There is no input that separates us" is.

## 6. Ground rules

The submission is the working tree at `/workspace/repo`. Commit or don't — only the
tree is collected, and everything below is about it.

- **No network.** Nothing fetches, and nothing you write may need to.
- **No new dependencies.** The three toolchains, glibc, and `make` are what there
  is. Do not vendor a library, and do not add a build-time generator beyond what
  upstream already has.
- **The build is the one in §2**, run from the repository root with no extra flags
  and no environment set. If your port needs something decided at build time, decide
  it inside the `Makefile` and the sources, from what the compiler already knows.
- **A build writes only under the repository root**, and writes the same set of
  files every time from a clean tree. A generated source file left behind, or a
  pre-generated blob with no rule that regenerates it, makes the byte order depend
  on which machine last ran `make`.
- **`quickjs.h` is a contract.** §3.8.
- **Don't try to detect that you are being graded**, and don't leave behaviour
  conditioned on it. A tree that answers differently depending on whether it thinks
  a test is watching fails the first stage outright, whatever else is true of it.

Scratch space outside the repository is yours. Use it for the oracle.

**Run the reference.** The `state-a` tag is the pristine tree, and every requirement
in §3 is stated relative to what it does — so you can measure your own work against
the same thing the grading measures it against:

```
git -C /workspace/repo archive state-a | (mkdir -p /tmp/oracle && tar -x -C /tmp/oracle)
```

Build that for all three targets and compare as often as you like. Do it before you
change anything, on `s390x` first: the failures §3.3 and §3.5 describe show up in
minutes, and seeing them is worth more than reading about them. When you think you
are done, do it again — a port that was never diffed against the reference is a port
done blind.

`git diff state-a` inside the repository is the other half of that: your own review
of what you changed, which is also what the first stage will be reading.

`quickjs.c` alone is fifty-four thousand lines, and the change this needs is not
large in comparison. Finding all of it is the work.
