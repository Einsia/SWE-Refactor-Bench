# Migrate zlib from C to Java

`/workspace/repo` holds **zlib 1.3.1**: a C library (`libz`), the public header,
a GNU version script, a CMake build, the packaging metadata for a release, and
the two upstream test drivers.

Your job is to make the whole thing Java, and to leave the compression itself
indistinguishable from the outside.

Concretely: after your change, the product is a **pure Java library** — one
modular jar, class-file version 61.0, built by the pinned JDK 17 from Java
sources in this repository. The C implementation must be gone from the
repository: not disabled, not kept behind a flag, not moved to a subdirectory.
And the library must produce **the same compressed bytes** as the C it replaces,
with the same return codes, the same error strings and the same observable
behaviour on every API path.

This is a rewrite of a working system across a language boundary that changes
what "the same interface" can even mean. There is nothing new to add. The
difficulty is entirely in reproducing what is already there, twice over: once in
the bytes, and once in the shape of an API designed for manual memory management
and out-parameters, re-expressed in a language that has neither.

Be clear about what "the same compressed bytes" means, because it is the whole
task. It is not enough that your deflate produces a stream some inflater
accepts. It has to produce **the stream zlib produces** — the same match choices
from the same hash chains and lazy evaluation, the same block boundaries, the
same Huffman trees, the same decision between stored, static and dynamic blocks,
bit for bit, at every level, strategy, window size and memory level. A correct
DEFLATE compressor that is not this one fails almost every case.

## The one thing that would make this trivial, and is forbidden

The JDK ships DEFLATE. `java.util.zip.Deflater` wraps the same zlib you are
being asked to replace, so it produces bit-identical output, and a three-line
delegation would pass a naive behavioural comparison outright.

**You may not use it.** Not `java.util.zip`, not `java.util.jar`, not directly,
not reflectively, not through a stream wrapper, not for CRC-32, not for Adler-32,
not "just for the gzip header". The ban is checked three ways, in three different
stages, because no one of them is sufficient on its own: a reviewer reads your
sources for it; the constant pool of every class in your jar is read for type
references *and* for string constants that name those packages; and your jar is
run on an instrumented JVM under a class loader that refuses to hand out
`java.util.zip`, to see what actually loads. A name assembled from two halves at
run time is invisible to the second and third and obvious to the first; an
implementation that reaches the JDK through a path nobody thought to read is the
reverse.

**Any one of the three is enough to end the evaluation.** The reading is a
required stage 1 gate, and stage 1 gates are pass/fail: failing one scores zero
and the later stages do not run. The other two are checks of a behavioural module,
and stage 2 pays only for a submission that passed every one of them: a single
failed check and the stage pays nothing, stage 3 does not run, and the other 60
points go with it. There is no version of this where delegating is worth a
deduction.

`java.base` is the only module you may require, and `java.util.zip` lives inside
it, so nothing about the environment prevents this. The rule is the task.

The same applies to reaching outside the JVM: no `System.load`, no
`System.loadLibrary`, no JNI, no `native` method, no `java.lang.foreign` linker,
no `Runtime.exec`, no `ProcessBuilder`, no bundled `.so`. There is no network.

## What the repository is

```
CMakeLists.txt          the build driver
zlib.h                  the public API and the behavioural specification
zconf.h.cmakein         template CMake configures into the installed zconf.h
zconf.h, zconf.h.in     the copies upstream ships for non-CMake builds
zlib.map                the GNU version script: 14 named version nodes
zlib.pc.cmakein         pkg-config template
zlib.3                  the man page
adler32.c compress.c crc32.c deflate.c gzclose.c gzlib.c gzread.c
gzwrite.c infback.c inffast.c inflate.c inftrees.c trees.c uncompr.c
zutil.c                 15 translation units
crc32.h deflate.h gzguts.h inffast.h inffixed.h inflate.h
inftrees.h trees.h zutil.h                 9 private headers
test/example.c          the upstream API test driver
test/minigzip.c         the upstream gzip-alike CLI driver
test/infcover.c         inflate coverage harness
doc/                    algorithm.txt and the three RFCs
win32/                  MSVC/MinGW makefiles and zlib1.rc
Makefile, Makefile.in, configure            the non-CMake build
ChangeLog FAQ INDEX README LICENSE
```

58 files, 18 C translation units, about 13,200 hand-written lines of C in the
top-level `.c` and `.h` files. `crc32.h` and `inffixed.h` are generated tables
rather than hand-written code; those tables still have to exist in your version,
whether you transcribe them or compute them at build time or in a static
initialiser.

Reading order that tends to work: `zlib.h` for the surface, then `deflate.c`
(the compressor, its configuration table, hash chains and lazy matching) with
`trees.c` beside it (Huffman construction, block emission, and the
stored/static/dynamic decision), then `inflate.c` with `inffast.c` and
`inftrees.c` (the decompressor, its fast path and its code-table builder).
`infback.c` is the callback-driven decompressor `inflateBack` exposes.
`gzlib.c`/`gzread.c`/`gzwrite.c`/`gzclose.c` are the stdio-alike `gz*` file API
over the same engine. `adler32.c` and `crc32.c` are the checksums, `compress.c`
and `uncompr.c` the one-shot helpers, `zutil.c` the small shared bits including
`zlibCompileFlags`. `doc/algorithm.txt` explains the compressor's design and is
worth reading before the code.

`git log` has one commit. The history is not available; the answer is not in it.

## The Java surface you must deliver

This is the part with no counterpart in a C-to-C port, and it is specified rather
than left open, because a grader cannot compare against an API it cannot name.
**`/opt/swerefactor/source-contract.json` is the normative statement** of the whole
thing: every class, every method signature, every constant, every mapping from a
C entry point to a Java member. Read it before you write code. What follows is
the shape of it.

Package **`org.zlib`**, module **`org.zlib`**, exporting `org.zlib` and requiring
`java.base` and nothing else. Seven public types:

| type | stands for |
|---|---|
| `Zlib` | the free functions: checksums, one-shot `compress`/`uncompress`, `version()`, `compileFlags()`, `errorString()`, `crcTable()` |
| `ZStream` | `z_stream` — the caller-visible stream state, plus the `Allocator`/`Deallocator` hooks |
| `Deflater` | the `deflate*` family |
| `Inflater` | the `inflate*` family |
| `InflateBack` | `inflateBack` and its `In`/`Out` callbacks |
| `GzHeader` | `gz_header` |
| `GzFile` | the `gz*` stdio-alike file API |

All 88 of State A's exported functions map onto members of those seven types, and
the contract's `symbol_map` gives the mapping for every one. The collapses are
deliberate and specified: `adler32`/`adler32_z` become one method because a Java
length is an `int` either way, `crc32`/`crc32_z` likewise, and the `*64` variants
of the `gz*` and combine entry points collapse into the `long` forms because Java
has no `z_off_t`/`z_off64_t` distinction.

Where C uses an out-parameter, the contract names the Java form — a one-element
`int[]` for in-out lengths, so `compress(byte[] dest, int[] destLen, byte[] src,
int srcLen)` reads the room available out of `destLen[0]` and writes the bytes
used back into it. Where C returns a status code, **so do you**: this API returns
`Z_OK`, `Z_STREAM_END`, `Z_BUF_ERROR`, `Z_DATA_ERROR` and the rest as `int`s, and
sets `ZStream.msg`. It does not throw on a compression error. Reproducing the
status-and-message discipline is part of the port; converting it to exceptions is
a different library.

The surface is closed in both directions: a member the contract lists and you do
not have is a missing member, and a public member you have and the contract does
not list is an addition. **A constructor counts, including one you did not
write.** `javac` gives a concrete public class with no declared constructor an
implicit *public* no-arg one, so the two types the contract lists with no
constructor — `Zlib`, whose members are all static, and `GzFile`, whose instances
come from `open`/`dopen` — need an explicit `private` constructor or they widen
the surface by default. `ZStream` is abstract and needs nothing. Members
inherited from `java.lang.Object`, and members the compiler synthesises for a
nested class, are exempt.

`zlib.h` **stays in the source tree** and its content must not change. It is no
longer installed — a C header advertises an ABI that no longer exists — but it
remains the behavioural specification, and the contract is written against it.

## What must be gone, what must stay

**Gone**: every `.c`, `.cc`, `.cpp`, `.s`, `.S`, `.o`, `.a`, `.so`, and every
private `.h` — all nine of `crc32.h`, `deflate.h`, `gzguts.h`, `inffast.h`,
`inffixed.h`, `inflate.h`, `inftrees.h`, `trees.h`, `zutil.h`. That includes all
18 translation units, three of which are under `test/`. (`zconf.h` is a public
configured header, not a private one; it is on the **May go** list below, so
either keeping it or deleting it is correct.) Also
gone: any `.class` or `.jar` committed into the source tree. Every byte the jar
executes must be compiled from source in your submission, during the graded
build. An out-of-source build directory is exempt, since the build produces it.

**Must stay**: `CMakeLists.txt` (as the build driver), `LICENSE` (byte-identical
— the port is a derivative work and the licence travels with it), `zlib.h`,
`README`, `ChangeLog`, and `doc/rfc1950.txt`, `doc/rfc1951.txt`,
`doc/rfc1952.txt`. The RFCs are the wire-format specification and are to the
bytes what `zlib.h` is to the API; a rewrite that deletes them has deleted the
evidence that its output is correct on purpose rather than by imitation.

**May go**: `Makefile`, `Makefile.in`, `configure`, `zconf.h`, `zconf.h.in`,
`zconf.h.cmakein`, `zlib.map`, `zlib.pc.in`, `zlib.pc.cmakein`, `zlib.3`,
`win32/`, `INDEX`, `FAQ`, `doc/algorithm.txt`, `doc/crc-doc.1.0.pdf`,
`doc/txtvsbin.txt`. `zlib.map` is the version script and `module-info` replaces
it; `zlib.pc.cmakein` is pkg-config and the jar manifest replaces it; `INDEX`
annotates the C translation units, so after the port it can only be wrong.
Removing any of these means removing what `CMakeLists.txt` says about it. State A
references `win32/zlib1.rc` on non-MinGW platforms and configures `zconf.h` from
`zconf.h.cmakein`, so deleting either leaves a reference behind. Deleting is
acceptable and keeping is acceptable — a dangling reference is not, because the
build must configure.

At least **3,000 lines of Java logic**. This is a floor against a thin wrapper,
not a target; a faithful port is larger.

## The build must keep working, exactly as invoked today

CMake stays the build driver (3.25 or newer; the graded toolchain is 3.25.1),
out-of-source, and both of these have to work from a clean checkout with no
network:

```sh
# shared
cmake -S . -B build-shared -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=ON -DZLIB_BUILD_EXAMPLES=ON \
      -DCMAKE_INSTALL_PREFIX=<prefix>
cmake --build build-shared --parallel
cmake --install build-shared

# static
cmake -S . -B build-static -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=OFF -DZLIB_BUILD_EXAMPLES=ON \
      -DCMAKE_INSTALL_PREFIX=<prefix>
cmake --build build-static --parallel
cmake --install build-static
```

Both configurations are graded independently and **both must install the full
inventory**. `BUILD_SHARED_LIBS` is meaningless for a jar, and that is the point:
an option a build no longer needs must still be accepted, because refusing it
breaks the build interface downstreams depend on. Upstream's `CMakeLists.txt`
already ignores it in practice — it declares `add_library(zlib SHARED ...)`
unconditionally — so preserving the behaviour is preserving what is there.
The CMake target names **`zlib` and `zlibstatic` must both still exist**; a
downstream `--target zlibstatic` has to keep working even though both now build
the same jar. Reconfiguring in an existing build directory and rebuilding a
second time with no changes both have to work.

Because both target names survive over a single artifact, and because the build
above is invoked with `--parallel`, **exactly one target may own the rule that
produces any given output.** Two `add_custom_target`s that each `DEPENDS` on the
same `add_custom_command` OUTPUT do not share one rule: the Makefile generator
has no target to attach the command to, so it copies the recipe into both
targets' `build.make`, and `make -j` may then run it twice at the same time. A
recipe that stages its output and renames it into place — which is what
assembling a jar correctly looks like — loses the staged file to whichever copy
finishes first, and the build fails intermittently. Give the rule one owning
target and have the other reach it with `add_dependencies`:

```cmake
add_custom_command(OUTPUT "${ZLIB_JAR}" ...)   # the rule, owned once
add_custom_target(zlib ALL DEPENDS "${ZLIB_JAR}")
add_custom_target(zlibstatic ALL)
add_dependencies(zlibstatic zlib)              # not DEPENDS on the same output
```

This is graded as `build/<config>/single-owner`, read out of the generated
makefiles straight after configure, so it does not depend on whether the race
fires during grading. It is a scored check like any other, and it is worth getting
right for a reason the weight does not carry: a build that corrupts its own artifact
under the command documented above is not a build that keeps working.

`ZLIB_BUILD_EXAMPLES` stays a **declared `option()`**, as it is today: it is
upstream's only one, and declaring it is what puts it in `CMakeCache.txt` as
`ZLIB_BUILD_EXAMPLES:BOOL`, which is what `cmake -LH` and every GUI and
packaging system reads to discover that the switch exists. Reading the variable
without declaring it builds the same thing and leaves the cache entry
`:UNINITIALIZED`, so the switch becomes invisible to everything that looks for
it — an interface change even though nothing about the build's behaviour moved.
The four `SKIP_INSTALL_*` switches are plain variables upstream tests with
`if(NOT ...)`, so they are not cache options and are graded by what they do.

The install tree must contain exactly:

```
share/java/zlib-1.3.1.jar
share/java/zlib.jar -> zlib-1.3.1.jar
```

and nothing else. No `include/zlib.h`, no `lib/libz.*`, no `.pc`, no man page, no
loose `.class` files: those describe a C library. The versioned filename with an
unversioned symlink beside it is the jar's version of a SONAME, and both are
required.

`javac --release 17` is the graded compile. The jar must be a **modular** jar
with `module-info.class` at its root, `META-INF/MANIFEST.MF` as its first entry,
every class under the `org/zlib/` prefix, and no `native` method anywhere in it.
Internal subpackages are fine — `org.zlib.internal` and the like are inside the
prefix and are not exported by the module descriptor, so the seven public types
stay the whole public surface. What the prefix rule excludes is a vendored
implementation relocated into some other namespace. Class files must carry a
`SourceFile` attribute — `javac` emits it by default, so do not compile with
`-g:none`. Multi-release jars are forbidden: a `META-INF/versions` tree ships
different bytecode per JDK, and only one of those would ever be graded.
`JAVA_HOME` is set, and the graded build resolves `javac` and `jar` through it, so
find the compiler the way a real build would rather than assuming a bare `javac`
on `PATH`.

Consumers must work in **both linkage modes**, and both are graded: on the module
path (`java -p <prefix>/share/java/zlib.jar --add-modules org.zlib`) and on the
class path (`java -cp <prefix>/share/java/zlib-1.3.1.jar`). They fail apart —
a jar with a malformed descriptor still works on the class path — so both are
checked.

`ZStream` and `GzHeader` are **not opaque**. Callers construct a `ZStream`, write
`nextIn`, `availIn`, `nextOut`, `availOut` and the allocator hooks into it, and
read `totalIn`, `totalOut`, `msg`, `dataType` and `adler` back out. The field
names and semantics are the interface. Only `GzFile`'s internals are private.

The two upstream drivers survive as Java, guarded by `ZLIB_BUILD_EXAMPLES` as
today: `org.zlib.test.Example` (upstream `test/example.c`) and
`org.zlib.test.MiniGzip` (upstream `test/minigzip.c`). Each must be launchable as
an **executable file named `example` and `minigzip` in the build directory** — a
shell wrapper around `java -p … -m …` is the expected shape, because the grader
execs a path rather than reading a `Main-Class`. They are not installed, and
their classes must not be inside the library jar: shipping test code in the
artifact is a jar-contract failure.

Upstream's `minigzip` reads `argv[0]` twice — to prefix its error messages, and to
decide from its own basename whether it was invoked as `gunzip` or `zcat`. A JVM
`main` is handed the arguments without the program name, so if you keep that
behaviour the wrapper has to pass it: a **system property** set by the launcher
(`java -Dzlib.prog="$0" …`, read back with `System.getProperty`) is the expected
mechanism, in these two driver classes only. It is accepted there precisely because
it is the JVM's calling convention showing through rather than a submission's
choice: the graded environment check reads the implementation sources and the jar,
and driver classes are in neither. `System.getenv` is not the same — reading the
environment is what the check is about, and in a driver it still draws a finding a
reviewer has to judge, so there is nothing to gain by preferring it. Inside the
library, both are a failure.

`example` also stays registered with CTest, as it is today: State A's
`CMakeLists.txt` calls `enable_testing()` and `add_test(example example)`, so
`ctest -N` in the build directory lists a test named `example`. That
registration is part of the build's interface — `ctest` is how a packager runs
the test suite without knowing what the test is — and it does not survive a
rewrite of `CMakeLists.txt` on its own.

## Behaviour: byte-exact, against the C implementation

The reference is **zlib 1.3.1 as built from the C sources you were given**.
Grading compares your behaviour to the reference's byte for byte over a large
generated corpus. Where the C implementation has a quirk, an oddity, or
behaviour the DEFLATE RFCs would arguably not require, **reproduce the quirk**.
Compatibility is defined against the reference implementation, not against
RFC 1950/1951/1952.

What is compared:

- **The compressed bytes**, over every combination of level (0–9 and
  `Z_DEFAULT_COMPRESSION`), strategy (`Z_DEFAULT_STRATEGY`, `Z_FILTERED`,
  `Z_HUFFMAN_ONLY`, `Z_RLE`, `Z_FIXED`), `windowBits` (9–15, the raw negatives,
  and the gzip `+16`/`+32` forms) and `memLevel` (1–9). This is the surface where
  a rewrite that is merely correct diverges from one that is compatible.
- **Streaming**, with verification input and output buffer splits down to one
  byte, and every flush mode (`Z_NO_FLUSH`, `Z_PARTIAL_FLUSH`, `Z_SYNC_FLUSH`,
  `Z_FULL_FLUSH`, `Z_BLOCK`, `Z_FINISH`) on varying schedules.
- **The whole exported API**: `deflateCopy`, `deflateReset`, `deflateResetKeep`,
  `deflateParams` mid-stream, `deflatePrime`, `deflateTune`, `deflatePending`,
  `deflateGetDictionary`, `deflateSetHeader`, `deflateBound`, `compressBound`,
  and on the other side `inflateBack`, `inflateCopy`, `inflateReset`,
  `inflateReset2`, `inflatePrime`, `inflateMark`, `inflateSync`,
  `inflateSyncPoint`, `inflateGetHeader`, `inflateGetDictionary`,
  `inflateCodesUsed`, `inflateValidate`, `inflateUndermine`. `deflateBound` and
  `compressBound` are compared as **numbers**, not merely as valid upper bounds.
- **Dictionaries**, on both sides, including the ones longer than the window.
- **Checksums**: `crc32`, `adler32`, `crcTable`, and every combine and
  combine-generator entry point.
- **The `gz*` file API**: `open`, `dopen`, `buffer`, `setParams`, `read`,
  `fread`, `write`, `fwrite`, `printf`, `puts`, `gets`, `putc`, `getc`, `ungetc`,
  `flush`, `seek`, `rewind`, `tell`, `offset`, `eof`, `direct`, `close`,
  `closeRead`, `closeWrite`, `error`, `clearerr`, and the file bytes they leave
  on disk.
- **The error surface**: return codes and the exact `msg` text for malformed,
  truncated and corrupt input, buffer exhaustion, and invalid parameters. The
  message strings are part of the interface.
- **Recovery**: `inflateSync` finding the next full-flush marker in a damaged
  stream, and how far it moves.
- **Custom allocators** through the `ZStream` hooks, including the allocation
  sequence. A JVM port has less to route through them than C does — `new
  Inflater()` *is* the state block C allocates — but the deferred inflate window
  is a real allocation and it is observed.
- **`Zlib.compileFlags()`**, which must return `0xa9` on this platform, and
  `Zlib.version()` / the `ZLIB_VERSION` and `ZLIB_VERNUM` constants.
- **`example` and `minigzip`**, rewritten in Java, still built by the graded
  CMake, still producing their reference stdout and exit status. These are the
  end-to-end artifacts: the grader runs your `example` and your `minigzip` and
  compares what they print and what they write.

Three conveniences worth knowing. zlib writes `MTIME=0` in the gzip header when
no `gz_header` is supplied, and `gzopen` does not call `time()`, so every gzip
stream is byte-comparable with no normalisation — do not "improve" this by
writing a real timestamp. The gzip `OS` byte is `0x03` on this platform and is
part of the compared bytes. And a handful of message strings come from the C
library's `errno` handling rather than from zlib itself; the contract lists what
is not graded, so you do not have to reproduce a `strerror` text through a JVM.

## What earns a score

Three stages, run in order, and each one decides whether the next happens. 100
points, all of them in the last two.

**Stage 1 — audit. Mandatory, worth no points, and able to end the
evaluation.** Whether this is a migration at all: whether the C implementation is
gone, whether the Java that replaced it is real, whether the compression comes out
of this repository rather than out of `java.util.zip` or a JNI binding or another
project's port, whether the build compiles C or reaches the network, and whether
anything in the tree behaves differently when it thinks it is being watched.

This stage is a reading, not a measurement. Both trees are mounted side by side,
the original and yours, and the reviewer's job is to decide whether one became the
other. There is no JDK and no CMake in that environment: nothing is built and
nothing is run, so what a file *is* matters and what it is named does not. A
finding has to rest on a path and a line in your tree. **If this stage decides the
migration did not happen, that is the whole result** — however well the library
works.

**Stage 2 — behavioural compatibility. 40 points, or none.** The byte-exact
comparison described above, over a generated corpus, grouped by area so that the
report tells you *where* a port is weak: the compressor families are much larger
than the rest, and a pooled number would let them speak for everything. The
grouping is reporting, not pricing. The 40 points are paid only for a submission
that passed every scored check in every area, so a port that matches the reference
at the common levels and drifts at `memLevel 1` is paid what a port that drifts
everywhere is paid. There is no near-miss that scores as nearly right.

Two things are worth stating separately. A default build that does not produce the
installed artifacts leaves nothing to measure, and the `java.util.zip` prohibition
at the top of this document holds here as well as in stage 1 — a cheat that no
input can detect does not cost less than one that every input catches.

**Stage 3 — verification. 60 points, and you cannot practise against it.** Six
independent attempts, each given the original C tree, your tree, and both built,
and each with an hour to write one input where the two disagree. A candidate
counts only if the original passes it, yours fails it, and it reproduces
identically three times. **Every attempt that finds nothing is worth 10 points to
you** — so this stage rewards the parts of the port nothing in stage 2 happened to
look at, and no amount of fitting to the visible cases substitutes for it. C
versus JVM differences that hold for any correct port do not qualify: unsigned
arithmetic, `size_t` width, allocation counts, `errno` text and iteration order
are excluded by rule, because they are what the platform is rather than what the
library answers.

Stage 3 is expensive, so it is only spent on submissions where the answer is
informative: **it runs only on a stage 2 at full marks** — every scored check
passed. Anything short of that and stage 2 pays nothing, so the final score is
zero and the 60 points here are never in play. Which makes the shape of the thing
worth stating plainly: a nearly complete port that fails one check scores what an
untouched repository scores, and the difference between that one check and none is
the whole 100 points. The reason is not severity for its own sake: if stage 2 has
already found an input
where you disagree with the C, six models spending an hour each to look for
another one buys nothing the behavioural report has not already handed you.

## Practical notes

- Work in `/workspace/repo` directly. Commit or don't; only the working tree is
  collected.
- Build and run things as you go. The C sources are right there, and you can
  build them, read them, and diff against them for as long as they remain in
  your tree. Getting byte-exact agreement *before* deleting the C is the short
  path; `test/example.c` and `test/infcover.c` are useful for this even though
  `infcover.c` is not itself graded and has no required Java counterpart.
- A real compiler is present in this environment because State A is C and you
  have to be able to build what you are replacing. The grading environment
  replaces it with a shim that refuses to compile anything belonging to this
  repository. Do not plan around the compiler being there at the end.
- The reference is the source you were given. When the RFCs and the C disagree,
  the C wins.
- Two Java-specific traps are worth naming in advance. C's `unsigned` arithmetic
  has no Java equivalent, and zlib relies on it everywhere: hash updates,
  checksum accumulation, `crc32_combine`'s polynomial arithmetic, the window
  index wraparound, and every comparison of a length against a bound. Getting
  `>>>` versus `>>`, and `Integer.compareUnsigned` versus `<`, right at each site
  is most of the porting risk. And C's `z_stream` aliases the caller's buffers
  by pointer while Java passes arrays with a separate offset; the offset
  bookkeeping that replaces pointer arithmetic is where the streaming cases fail.
- Expect the long tail to be in the compressor. The configuration table in
  `deflate.c`, `longest_match`'s `nice_match` and `max_chain` cutoffs, the
  `TOO_FAR` rejection of distant short matches, `deflate_rle` and `deflate_huff`,
  `_tr_flush_block`'s three-way block choice, `build_tree`'s tie-breaking and its
  code-length limiting pass, and `deflate_stored`'s direct-copy path each have
  behaviour that is easy to get almost right — and almost right is a byte
  difference.
