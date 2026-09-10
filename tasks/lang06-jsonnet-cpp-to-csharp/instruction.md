# Migrate jsonnet from C++ to C# / .NET 8

You are working in `/workspace/repo`. It holds the source of **jsonnet 0.20.0**, the
Google configuration language, implemented in C++11 and built with GNU Make.

Your task is to **replace the C++ implementation with a C# implementation targeting
.NET 8**, so that the repository's two command-line programs behave exactly as they
do today while no C or C++ source remains.

This is a repository-level migration, not a port of one file. Expect it to take a
long time. Read the existing implementation; it is the specification.

---

## What must exist when you are done

Two command-line programs, produced by `dotnet publish` of projects in this
repository, as **managed .NET 8 assemblies**:

| program | role | filenames it accepts |
| --- | --- | --- |
| `jsonnet` | evaluate a Jsonnet program and print the result | exactly one |
| `jsonnetfmt` | reformat Jsonnet source, preserving comments | zero or more |

Both must keep their current command-line surface, their current output, their
current error text and their current exit statuses.

`jsonnet` accepts these flags today, and must still accept them:

```
--exec/-e            --ext-str/-V         --ext-str-file       --ext-code
--ext-code-file      --tla-str/-A         --tla-str-file       --tla-code
--tla-code-file      --jpath/-J           --output-file/-o     --multi/-m
--string/-S          --yaml-stream/-y     --max-stack/-s       --max-trace/-t
--gc-min-objects     --gc-growth-trigger  --version            --help/-h
```

`jsonnetfmt` accepts these:

```
--exec/-e            --output-file/-o     --in-place/-i        --test
--indent/-n          --max-blank-lines    --string-style       --comment-style
--pretty-field-names --pad-arrays         --pad-objects        --sort-imports
--debug-desugaring   --version            --help/-h
```

The flags that read a value take it the way they take it now, including the
`--flag=value` and `--flag value` forms where those are accepted today, the
`var=value` and bare-`var`-from-environment forms of the external and top-level
argument flags, `-` meaning standard input, `--` terminating flag parsing, and the
`--no-` prefix on the boolean formatter flags that support it.

Where behavior is not obvious, the C++ code in `core/` and `cmd/` is authoritative.
Read it rather than guessing.

---

## Preserve exactly

**`stdlib/std.jsonnet` must not change.** It is the Jsonnet standard library,
written in Jsonnet. It is data, not C++, and translating it to C# is *not* part of
this task: embed it unmodified and evaluate it with your ported evaluator — subject
to the override described immediately below, which decides what forty of its names
actually do. Its sha256 is checked, byte for byte.

Embedding it is necessary but not sufficient, and the trap here is worth spelling
out. After parsing `std.jsonnet` the reference **overwrites** field bodies in the
resulting object: for every entry in `jsonnet_builtin_decl`
(`core/desugarer.cpp:40-88`) it does `field->body = fn`, replacing whatever Jsonnet
source defined that name with a native implementation. Forty names come from C++
this way — the thirty-nine declared builtins plus `std.thisFile`, which the same
code injects as a hidden string field.

Seven of them are the reason this matters: `join`, `substr`, `range`, `strReplace`,
`asciiLower`, `asciiUpper` and `splitLimit` all have a perfectly good Jsonnet body
in `std.jsonnet` that the reference **never executes**. If you embed the library and
evaluate it as written, those bodies run in your port and the reference's C++ runs
upstream, and the two do not agree. `std.jsonnet`'s `join` raises its type error
with a trailing space; the C++ at `core/vm.cpp:1725` does not, and the expected
output is the C++ one. Forty-three of the 102 fields in `std.jsonnet` reach one of those
seven — the seven themselves and thirty-six more, among them `format`, `split`,
`lines`, every `manifest*` and four of the five `escapeString*` — so this is not a
corner.

Implement all forty natively and apply the same override, or match the natives'
observable behavior exactly some other way. Preserving `std.jsonnet` byte for byte
is about the file on disk; it is not permission to let its Jsonnet bodies decide
what these forty names do.

Keep as well:

- `test_suite/`, `examples/`, `doc/` — the conformance suite, the documented
  examples and the language reference
- `LICENSE`, `README.md`, `CONTRIBUTING`, `release_checklist.md`

`README.md` should be updated to describe how the project is built now. Preserving
a file means preserving its content and intent, not freezing stale build
instructions.

---

## Remove entirely

The C++ implementation and its build systems must be gone from the tree:

- `core/`, `cmd/` — the implementation and the two CLI drivers
- `include/`, `cpp/`, `python/` — **out of scope, and must be removed.** The C ABI,
  the C++ binding and the CPython extension are not part of what you are asked to
  reproduce. Do not port them; delete them.
- `third_party/`, `vs2017/`
- **Every build description for the C++ implementation — at any depth, including
  inside a directory you must otherwise preserve.** That is `Makefile`,
  `CMakeLists.txt`, `CMakeLists.txt.in`, and every Bazel file (`BUILD`,
  `BUILD.bazel`, `WORKSPACE`, `WORKSPACE.bazel`, `*.bzl`). Three of them sit in
  directories that are otherwise kept, and are named here so that the depth rule
  is not something you have to infer: **`stdlib/BUILD`, `stdlib/CMakeLists.txt`
  and `test_suite/CMakeLists.txt`**. A build file that survives but no longer
  builds anything — because the sources it names are gone, or because no root
  graph reaches it — still counts as remaining, and is graded as remaining.
  `case_studies/fractal/Makefile` is not one of these and stays: it runs the
  `jsonnet` CLI over a demo and names no compiler and no C++ source.
- `setup.py`, `MANIFEST.in`, `stdlib/to_c_array.cpp`, `Dockerfile`,
  `.travis.yml`, `tests.sh`

No `.c`, `.cc`, `.cpp`, `.cxx`, `.h`, `.hh`, `.hpp`, `.hxx`, `.inc` or `.ipp` file
may remain as implementation source. Three directories are exempt because what
they hold is not implementation source: `test_cmd/` (golden *output* files whose
names happen to end in `.cpp`), `case_studies/` (an unrelated Mandelbrot demo) and
`doc/` (documentation assets).

Do not leave compiled binaries behind. No ELF or PE file may be retained in the
source tree.

---

## Build and run

The .NET 8 SDK is installed. **There is no network**, at any point, for you or for
grading.

- Build and publish with `dotnet build` / `dotnet publish -c Release`.
- The implementation must compile against the **.NET base class library alone**.
  No external packages.
- **Each CLI must be produced by an executable project — `<OutputType>Exe</OutputType>`
  — whose assembly name is exactly `jsonnet` or `jsonnetfmt`.** Grading locates the
  two programs by the assembly each project produces: `<AssemblyName>` when you set
  it, otherwise the `.csproj` filename. Where the projects live and what the files
  are called is up to you — `src/Jsonnet.Cli.Eval/Jsonnet.Cli.Eval.csproj` with
  `<AssemblyName>jsonnet</AssemblyName>` and `src/jsonnet/jsonnet.csproj` are both
  found. A project that produces `jsonnet-cli` or `JsonnetEval` is not, and neither
  is a class library that happens to be named `jsonnet`.
- `NUGET_PACKAGES=/opt/nuget-offline` is pre-seeded with test frameworks only
  (xunit, NUnit, Microsoft.NET.Test.Sdk, FluentAssertions). Use them for your own
  tests if you want them; the implementation may not depend on them.
- Set `InvariantGlobalization`. Number and string formatting must not depend on
  ICU or on the ambient locale.

The C++ toolchain is also installed, on purpose: `make jsonnet jsonnetfmt` builds
the current implementation. Use it as a reference to diff against while you port.
Delete the binaries and the build system when you are done.

Grading publishes your projects from source with `PublishSingleFile`,
`PublishAot`, `PublishTrimmed`, `PublishReadyToRun` and `SelfContained` **all
forced off**, so the assemblies stay inspectable. Do not rely on any of those
being on.

---

## One rule worth stating outright

Every number either program prints goes through a four-line routine in
`core/parser.cpp` (`jsonnet_unparse_number`):

```c++
if (v == floor(v)) { ss << std::fixed << std::setprecision(0) << v; }
else               { ss << std::setprecision(17) << v;              }
```

In C terms: **`"%.0f"` for integral values, `"%.17g"` for everything else.**

No .NET format string reproduces this. `"R"` and `"G17"` both switch to `E+xx`
notation for large values and drop the seventeenth digit when sixteen round-trip;
`"F0"` mangles the small ones. Consequences you are responsible for:

- Integral values never use exponent notation at any magnitude. `1e100` prints as
  101 digits — the exact decimal expansion of the double, not a rounded one.
- `%.17g` keeps 17 significant digits and strips trailing zeros: `0.1` prints as
  `0.10000000000000001`, `0.5` prints as `0.5`.
- `%.17g` switches to exponent form below `1e-4`: `0.0001` stays decimal,
  `1e-5` becomes `1.0000000000000001e-05`.
- The exponent is signed and at least two digits: `e-05`, never `e-5`.
- `-0.0` prints as `-0`.

Every byte of output depends on getting this right. Implement the rule.

---

## Errors

Three error channels, each with a fixed prefix and exit status:

| channel | prefix | exit |
| --- | --- | --- |
| command line | `ERROR: ` | 1 |
| static (parse, syntax, unbound) | `STATIC ERROR: ` | 1 |
| runtime | `RUNTIME ERROR: ` | 1 |

`jsonnetfmt --test` exits 2 when the input is not already formatted. Success is 0.

Runtime errors carry a stack trace: tab-indented frames, tab-separated fields,
`...` where the trace is elided by `--max-trace`, and the last frame's context
left empty. Match the existing text exactly — messages, punctuation, whitespace
and frame layout are all compared.

---

## Two places where the reference cannot be matched, and is not asked to be

"Behave exactly as it does today" is the standard everywhere except two surfaces
where 0.20.0 is broken. Both are excluded from what is measured. They are written
down here because the alternative is a document that asks for something it does not
grade, and a port that implemented either one *correctly* would differ from the
reference and look wrong.

**`jsonnetfmt --debug-desugaring` fails on every input**, including a file
containing `1 + 1`:

```
STATIC ERROR: std.jsonnet:975:21-25: Truncated escape sequence in string literal.
```

Desugaring rewrites every string literal to the double-quoted kind and decodes it
once; the formatter's string-style pass then decodes it again. The stdlib is spliced
into every program, so the failure does not depend on your input at all. The flag
must still be **accepted** — a port that rejects it as an unrecognised argument is
wrong, and that much is graded — but its output is not compared.

**A JSON or YAML number literal that overflows a double aborts the reference
parser.** `std.parseJson("1e400")` and `std.parseYaml("1e400")` raise SIGABRT;
`1e308` is fine and so is underflow to `1e-400`. Inputs that overflow are excluded
from the graded surface, as a rule rather than as a list of strings. Note the
asymmetry, because it limits the exclusion to the two parsers: the same magnitude
written as a *source* literal is an ordinary static error and exits 1, so the
language surface is unaffected and is graded normally.

An abort is not a behaviour, and neither is a spurious error from a double decode.
Do whatever is reasonable on these two, including nothing.

---

## How the result is judged

Three things happen to the repository you leave behind, in order, and each of them
can end the run.

First it is **read**. The C++ you were given and the tree you leave are compared,
by a reviewer who may open any file in either and may build nothing, to decide
whether the rewrite described above actually happened: whether the interpreter
left `core/` and reappeared in C#, or was wrapped, vendored, shelled out to,
answered from a table, or kept as C++ under another name. The two questions it
presses hardest are the two that a passing test suite does not answer — is there
an evaluator in here, and is `std.jsonnet` being interpreted or was it quietly
hand-translated into C#.

Then its **behaviour** is measured. Both CLIs are published from your source in a
clean container and run the way a user runs them — argv, stdin, files on disk —
and stdout, stderr, exit status and the files each run touched are compared byte
for byte against the C++ reference, over the surface described above. It is
reported by capability rather than by case count — the operator matrix holds more
cases than anything else and does not speak for everything else — so the report
tells you which parts of the language you have finished. The points are not
divided that way: they are paid only for a submission that passed every scored
check, so a port that gets the evaluator, the standard library and the formatter
right and misses a corner of YAML parsing is paid exactly what a port that gets
neither is paid.

Then it is **attacked**. Independent attempts are made to write an input the C++
handles and yours does not — same shape as the comparison above, both
implementations built and run side by side. This stage is the larger of the two
that award credit, and it is only reachable by a port that agrees with the
reference everywhere: a submission that fails any of the measured surface stops
before this stage with nothing. So agreement with the reference is not most of the
score, it is the price of admission to most of the score — and the way to earn it
is breadth of agreement rather than anticipating an adversary you may never be
shown.

There is no partial credit, and no list of strings to avoid. Write the
implementation you would ship, and finish it.

### What the reading stage is actually looking for

A thin C# shell around `libjsonnet` passes every behavioural case while doing none
of the work. So does a program that shells out to a retained `jsonnet`, loads a
native library at run time, or recognises its input and prints a stored answer.
Those are the shapes that get a submission zero, and the question asked about each
of them is whether a path exists in your source by which the C++ still produces
the result — a question answered by reading the code, not by matching patterns in
it, so spelling a `DllImport` target at run time or splitting a literal changes
nothing about the outcome.

What your published assemblies declare is a separate matter, measured once there is
something built to inspect: their CLR metadata is read directly, and an assembly
that declares a P/Invoke, references a native module, or reaches for a
process-creation API is reported as declaring it. Managed code that uses spans or
pointers for its own arithmetic is an ordinary C# implementation and is not that.

Everything else is an ordinary engineering judgement and is treated as one. An
idiomatic C# design is the point of the exercise: organise the code as you would
organise C#, not as a transliteration of the C++. Nothing checks your namespaces,
your type or member names, your file layout, or whether any particular variable
exists — only what the two programs do. The single exception is the one stated
under **Build and run**: the two published assemblies must be named `jsonnet` and
`jsonnetfmt`, because that is how grading finds the programs to run at all. What
the projects and files around them are called remains yours.

### The conformance data, which stays in your tree

`test_suite/`, `test_cmd/` and `examples/` are upstream's own conformance data, and
they are preserved paths — they stay exactly where they are. Their `.golden` files
hold the expected output of cases your evaluator is measured on.

Keep them, port the upstream runner, write your own test project around them: all
of that is normal work on this repository and none of it counts against you. The
line is what your **implementation** does. It may not take its answers from them —
not by reading them while it evaluates, not by compiling their text in, not from a
resource, not behind a hash. An evaluator consults the program it was given; it does
not consult a record of what the answer was last time.

Grading does not read your copy of any of it. Every graded input is materialised
from a frozen copy of State A, so editing, deleting or rearranging the conformance
data changes nothing about your score in either direction — which is exactly why
there is nothing to gain by touching it, and why leaving it alone is free.

The same principle covers your build, since a build can reach a file without any
source naming it. And it covers the container your programs run in during grading,
which is the verifier's rather than yours: the filesystem around them at that moment
is not part of your repository, there is nothing in it that would help — the
expectations are read and closed before your build runs — and a program that goes
looking through its surroundings instead of evaluating its input has answered the
question this whole exercise asks.

## Practical notes

- Work in `/workspace/repo` directly. Commit or don't; only the working tree is
  collected, and build debris is dropped when it is — `bin/`, `obj/`, publish
  prefixes, object files, archives, shared objects, executables. Everything your
  build needs has to be in tracked source, because grading republishes from that
  source in a container where none of your build output exists.
- No solution file is required, and no particular directory layout. Each CLI
  project is restored and published on its own, by the assembly name it produces.
- The offline package folder is a **closed set**, not merely an unreachable
  network: no package source is configured, at development time or at grading
  time, and a package that is not already seeded fails to restore. Adding a
  `nuget.config` of your own does not open it. Build against the base class
  library.
- Build and run things as you go. The C++ is right there and `make jsonnet
  jsonnetfmt` works — getting agreement with the reference before you delete the
  C++ is the short path.
- The reference is the source you were given. Where the documentation and the C++
  disagree, the C++ wins.
- Expect the long tail to be where the time goes: number formatting, error
  messages and their stack traces, object inheritance with `super`, laziness that
  has to stay lazy, and the formatter's handling of comments each have behaviour
  that is easy to get almost right.

`/opt/swerefactor/source-contract.json` states the mechanical parts of this document in
machine-readable form — the flag matrices, the number rule, the exit statuses, the
preserved paths, the forbidden paths, the build requirements, and the two exclusions
above. It says nothing about how any of it is scored. It is read-only and outside the
repository, so it is not part of what you submit. Where this document and that file
could be read differently, that file is the one the reading and behaviour stages both
check their own copy of, and it is what they enforce.
