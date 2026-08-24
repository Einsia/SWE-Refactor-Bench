# Rewrite JSONata from JavaScript to TypeScript

You are working in `/workspace/repo`. It holds **JSONata 2.2.2** — a query and
transformation language for JSON, and its reference implementation: a lexer, a Pratt
parser, a tree-walking evaluator with tail-call handling and a configurable stack
limit, a builtin library of about a hundred functions, an XPath-style picture-string
datetime formatter with its own ISO-week arithmetic, and a signature validator that
type-checks calls at run time from a compact letter grammar.

Your task is to **replace the JavaScript implementation with a TypeScript one**, so
that the same requests produce the same bytes while no JavaScript implementation
remains.

Fifteen files, 9,706 lines. Nine of them are the implementation:

| path | what it is |
| --- | --- |
| `src/jsonata.js` | the evaluator: the tree walk, tail calls, the stack limit, lambdas, partial application, the transform operator |
| `src/parser.js` | the lexer and the Pratt parser, including error recovery under `recover: true` |
| `src/functions.js` | the builtin library — about a hundred functions, several with argument-count-dependent behaviour |
| `src/datetime.js` | `$fromMillis`/`$toMillis` and the picture-string formatter, with its own ISO week-date arithmetic and roman numerals |
| `src/signature.js` | the run-time signature validator: a letter grammar compiled to a matcher that reports argument positions |
| `src/utils.js` | sequence and singleton semantics, deep equality, the hashing the engine relies on |
| `src/probe.js` | the differential probe's driver. Speaks the protocol below. |
| `src/probe-wire.js` | the response encoder. **It decides bytes, and bytes are what is compared.** |
| `src/probe-fixtures.js` | the closed vocabulary of host callables and regex engines the protocol exposes |
| `tools/build.js` | a zero-dependency build: copies `src/*.js` to `dist/`. This is what you are replacing with `tsc`. |
| `jsonata.d.ts` | hand-written declarations for the public API. **Not a deliverable** — see below. |
| `README.swerefactor.md` | the probe protocol's own specification, in prose. Read it. |
| `README.md` | upstream's own documentation |
| `package.json` | scripts and metadata. No dependencies. |
| `LICENSE` | JSONata's MIT licence |

The six library modules are 5,665 logic lines; the three probe files are 711. Under
`src/` that is 358,338 bytes across 9,048 lines, of which **6,376 are JavaScript that
is neither blank nor comment**. That number is the specification's size, not a target
for yours.

There is **no `tsconfig.json` here at all**. You are producing that side from nothing.

Read the JavaScript. It is the specification, and it is a *runnable* one: `npm run
build && node dist/probe.js` answers requests on stdin, offline, in this container.
Any question this document leaves you unsure about — "what exactly does this
expression return?", "which code does that error carry?" — can be settled by asking
the original instead of guessing. Do that early and often.

Nothing about the original's presence is held against you. It is here to be consulted.
What is graded is whether the **delivered implementation** is TypeScript that you
wrote, rather than the original wearing a new extension.

---

## What must exist when you are done

| path | requirement |
| --- | --- |
| `package.json` | empty `dependencies` **and** `devDependencies`; a `build` script that is a `tsc` invocation on `tsconfig.json` |
| `tsconfig.json` | the options in the next table. You author this file; there is none in the tree. |
| `src/*.ts` | the port. How many files, and what is in each, is yours. |
| `dist/probe.js` | what the build produces, and what every stage runs |
| `dist/jsonata.js` | the library, requirable on its own: `require('./dist/jsonata.js')` gives the callable |
| `dist/jsonata.d.ts` | the declarations, **emitted by `tsc` from your source** |

The grader runs `npm run build` from the repository root, with no arguments, and then
`node dist/probe.js` the same way. A build that needs another command first has not met
this. Those three `dist/` paths are hard-coded in every stage, so they are not yours to
choose; everything else about `dist/` and all of your `src/` layout is.

`dist/` is where `tsc` writes, so **`dist/` is full of JavaScript, and that is
required rather than tolerated**. Nothing in this task asks you to remove emitted
JavaScript. What it asks is that the *sources* under `src/` are TypeScript.

`tsconfig.json` must set these, and they are checked:

| option | value |
| --- | --- |
| `strict`, `noImplicitAny`, `strictNullChecks`, `noEmitOnError`, `declaration` | `true` |
| `rootDir` | `src` |
| `outDir` | `dist` |
| `module` | `commonjs` |
| `target` | `ES2022` |
| `types` | `[]` |

`rootDir` and `outDir` are what put the three fixed paths at the three fixed places.
`module` and `target` are what make the emitted JavaScript requirable by the harness.
`"types": []` keeps the build honest about having no `@types` packages: unset, `tsc`
resolves every type package it can find, and this image has none.

`declaration: true` is required rather than merely allowed, because `dist/jsonata.d.ts`
has to be *generated from the ported source*. Copying `jsonata.d.ts` across, or
hand-writing a replacement, produces a declaration file that describes what you meant
instead of what you wrote — which is the one thing a declaration file exists not to do.

Do not relax the strictness flags, and do not route around them: a file-wide
`// @ts-nocheck`, `// @ts-ignore` used to silence ported logic in bulk rather than to
document one real limitation, `allowJs` with the implementation still in `.js`, an
`include` list narrowed so the large modules are never compiled, or declaring the
engine's own surface as `any` are all the same evasion. Nor a bundler, `--noCheck`, or
a transpile-only path in place of `tsc`.

`any` itself is **not** forbidden, and deliberately so — JSONata values are arbitrary
JSON, and a port that models them honestly will reach for `unknown` and `any` in
places. What is forbidden is using it to make strict mode vacuous.

---

## The probe protocol

**This is the entire graded interface.** JSONata's JavaScript API is not graded
directly: a TypeScript port is free to shape its own internals however it likes, and
what is compared is what comes out of the probe.

`README.swerefactor.md` in the repository root is this protocol's full specification, and
`src/probe.js` is a runnable copy of it. **Read both.** What follows is the part you
cannot discover by experiment, plus the parts most easily got subtly wrong.

NDJSON in, NDJSON out: one JSON request per line on stdin, **one JSON response per line
on stdout, in order, until stdin reaches EOF, then exit 0**. Three modes:

| invocation | behaviour |
| --- | --- |
| `node dist/probe.js` | read stdin, write stdout |
| `node dist/probe.js --batch <in> <out>` | read a file, write a file |
| `node dist/probe.js --selfcheck` | reach every op once, exit 0 |

The two driving modes are graded against each other: `--batch <in> <out>` must write to
the output file exactly what the same requests on stdin write to stdout. They exist
separately because expectations were captured to a file and grading runs over a pipe; a
probe whose modes disagree has a buffering bug, and that bug is worth points here.

Four transport rules that are each a graded case:

* **One line out per line in**, including for a line that is not JSON — that draws a
  `P0001`. A blank line mid-stream is such a line and is answered; a trailing newline
  at end of stream is framing and is not.
* **Each response reaches the far side before the next request is read.** Not buffered
  until exit. A probe that dies on request 300 has delivered 299 answers and is graded
  on them; a probe that buffers loses all of them.
* **Nothing else on stdout.** Diagnostics go to stderr, which is not compared.
* The probe **must speak the protocol whatever the working directory is**. It is run
  from elsewhere.

### The six ops

| op | required | optional |
| --- | --- | --- |
| `"hello"` | — | — |
| `"ast"` | `expr` | `recover` |
| `"eval"` | `expr` | `input`, `bindings`, `options`, `clock`, `repeat` |
| `"evalcb"` | `expr` | `input`, `bindings`, `options`, `clock`, `repeat` |
| `"assign"` | `expr`, `assigns` | `input`, `options`, `clock`, `repeat` |
| `"register"` | `expr`, `funcs` | `input`, `options`, `clock`, `repeat` |

A field the op does not declare is a `P0006` rather than something to ignore. Two
details that are easy to lose and are graded:

* `input` absent and `input` present as `null` are **different requests**. JSONata
  distinguishes an absent input from a null one, so the probe must too.
* `repeat` evaluates *on one compiled expression object* and reports every answer.
  That is how per-evaluation state becomes observable, and a port that recompiles per
  repeat gives different answers to `$random()` and to the `counter` fixture.

Responses are `{"id":<int>,"ok":true,"op":"<op>","result":<body>}` or
`{"id":<int>,"ok":false,"op":"<op>","error":<error>}` — keys in exactly that order. A
request that fails before its op is known still gets an answer, with `id` falling back
to `0` and `op` to `""`. That is the only case where either is invented.

`hello` reports the closed vocabulary and nothing about the implementation behind it —
no version, no runtime, no build date:

    {"engines":[...],"impls":[...],"ops":[...],"protocol":"jsonata-probe/1"}

Keys in that order, each list sorted. Reproduce it exactly.

### The response encoder is part of the protocol

`src/probe-wire.js` decides bytes, and **bytes are what is compared**. Of everything
here it is the file to port most carefully, because a plausible-looking encoder
disagrees on every case rather than on one:

* Key order is fixed, not alphabetical-by-accident. `JSON.stringify` on a plain object
  emits insertion order — so insertion order is a contract.
* No whitespace anywhere outside strings. No BOM. LF line endings. UTF-8.
* A JSONata sequence is a JavaScript array carrying hidden flags. Singleton sequences,
  the keep-array flag and the difference between a sequence and a plain array are all
  observable through this encoder, and reproducing them is most of what makes a port
  byte-exact rather than nearly so.
* `undefined` is a value here. An expression that matches nothing yields it, and it is
  **not** the same as `null` on the wire.
* Numbers come back the way the runtime prints them, exponent form and all. You are on
  the same runtime as the original, so this is free — as long as you do not reformat.

### Errors

Three kinds, in the `kind` field:

| kind | when |
| --- | --- |
| `JsonataError` | the library raised a coded error. `code` is its code, `message` its message. |
| `ProtocolError` | the request was not usable. `code` is a `P00NN` and no library call happened. |
| `Panic` | something failed with no code — an exception the library did not raise deliberately. |

Field order inside the error object: **`kind` first, `message` last**, and in between
only the fields that are present, in this order: `code`, `position`, `token`, `value`,
`index`, `type`. There is deliberately no stack trace.

The ten protocol codes, all of which the corpus reaches:

| code | condition |
| --- | --- |
| `P0001` | the line is not valid JSON |
| `P0002` | the request is not a JSON object |
| `P0003` | the request has no integer `id` |
| `P0004` | the request has no string `op` |
| `P0005` | unknown op |
| `P0006` | unexpected field |
| `P0007` | field has the wrong type |
| `P0008` | required field is missing |
| `P0009` | unknown fixture name |
| `P0010` | unusable option |

Which of two bad fields is reported depends on the order the validator checks them,
and that order is observable. Read `src/probe.js` for it rather than inferring it.

The engine's own diagnostics are a larger surface: **101 declared codes**, each
carrying a position, a token and sometimes a value. The corpus reaches 96 of them. A
port that produces the right failure with the wrong code, position or token is wrong on
that case, and there are many such cases.

---

## The clock, the fixtures, and four sharp edges

`$now()`, `$millis()` and `$random()` are shadowed before every evaluation through
`registerFunction` — the same route any consumer would take, so a port that implements
`registerFunction` correctly inherits the pinning for free. `$millis()` is
`1700000000000`; `$random()` is a `mulberry32` generator seeded with `0x9E3779B9` and
re-seeded per evaluation, written out in exact 32-bit operations in
`src/probe-fixtures.js` because "a seeded PRNG" is not a specification.

A request may opt out with `"clock":"live"`. Those responses are not compared against
anything — they carry the wall clock — and the option exists so the pinning is
observable rather than assumed.

The fixtures are a closed vocabulary of **13 host callables** and **4 regex engines**,
defined in `src/probe-fixtures.js` and reported by `hello`. Four carry a requirement
that is easy to miss and expensive to find:

* `focusInput` and `focusLookup` read `this.input` and `this.environment.lookup`. A
  registered function is called with the evaluation focus as its receiver; a port that
  loses that binding fails both. Under `strict`, typing `this` is the interesting part.
* `asyncDouble` returns a promise, which the library awaits — so the answer is the
  resolved value. A port that reports the promise itself encodes an `x`.
* `counter` holds state across calls within one registration, and is rebuilt per
  registration rather than shared between them.
* `hostDate` and `hostMap` return objects the library has no type for. They are the only
  values in reach of the `x` tag, and what the library does *around* them is not
  uniform: `$type` says `object`, `$boolean` is false, `$count` is 1, `$keys` is
  undefined, and `$string` differs between the two.

The fifth `options` key, `engine`, is this protocol's stand-in for the library's
`RegexEngine` option, which is a constructor and cannot travel over a wire. One sharp
edge there, worth stating because it costs an afternoon: what the library hands a
`RegexEngine` constructor is the `RegExp` **object the parser built**, not a pattern
string, and it already carries the `g` flag. An engine that rebuilds it must merge flags
rather than replace them — dropping `g` means `lastIndex` stops advancing and the match
iteration never terminates.

---

## Where JSONata is quirky, reproduce the quirk

The reference is JSONata 2.2.2 as it ships here, built by the pinned toolchain.
Comparison is byte-exact on stdout, per response. Where the original is quirky,
arguably wrong, or plainly buggy, **the port reproduces the behaviour**. There is no
credit for fixing it and there are cases that will catch you doing so.

The ones that account for most near-misses:

* **Sequences are flagged arrays.** Singletons, the keep-array flag, and the difference
  between a sequence and a plain array are all observable.
* **`undefined` is a value**, distinct from `null`, and it is what an expression that
  matches nothing yields.
* **Numeric output is JavaScript number formatting**, exponent form included.
* **Validation follows request key order**, so which of two bad fields is reported is
  observable.
* **Signature errors report argument positions** — the index of the offending argument
  is in the diagnostic.
* **The datetime formatter has its own ISO week-date arithmetic**, roman numerals, and
  picture-string edge cases that do not match any library you might reach for.

### What is not graded

Three things, and each is a place a careful porter can spend hours for no points:

* **Internal idioms.** Class and function names, whether something is a closure or a
  method, how many files `src/` has and what is in each — all yours, with the sole
  exception of the three fixed `dist/` paths.
* **Performance.** The timeouts are one to two orders of magnitude above what the
  reference needs, deliberately. A correct port that rebuilds an AST on every
  evaluation is still a correct port.
* **The wall clock, `$random()` under `"clock":"live"`, iteration order where the engine
  does not fix it, and the depth at which the runtime exhausts its stack.** These are
  excluded from the corpus.

---

## Keep, remove, and what must not be there

Four paths exist now and must still exist, with the stated property:

| path | why |
| --- | --- |
| `LICENSE` | JSONata's MIT licence. Removing or altering it is a licence violation, not a cleanup. |
| `package.json` | still the manifest, with empty `dependencies` and `devDependencies` and a working `build` script |
| `README.md` | upstream's documentation. It describes the API; keep it describing the API you ship. |
| `README.swerefactor.md` | the protocol specification. It is the definition the grader's cases were written from. |

These are the JavaScript whose purpose ends with the port, and removing them is
expected: the nine `src/*.js`, `jsonata.d.ts`, and `tools/build.js`. A submission that
ported everything correctly and left `src/*.js` sitting beside the TypeScript has not
finished — that is what stage 1 asks about.

Paths that must not exist anywhere under the repository:

| pattern | why |
| --- | --- |
| `.node`, `.wasm`, `.so`, `.dylib`, `.dll`, `.exe` | a compiled artifact is a way to ship an implementation nobody can read |
| `node_modules/` | a committed `node_modules` is how a third-party `jsonata` arrives without appearing in `package.json` |
| `*.js.bak`, `*.js.orig`, `*.ts.orig`, `*.orig`, `*.rej` | where the original goes to hide during a rename |
| `npm-shrinkwrap.json` | a lockfile for dependencies there are none of |

`dist/` is the exception and is **not** forbidden: it is where the build writes, it is
full of emitted JavaScript, and it must contain the three fixed paths. Do not add it to
`.gitignore` reasoning and do not delete it as cleanup.

---

## Build and run

    npm run build          # tsc over your tsconfig.json, writing dist/
    node dist/probe.js     # the graded entry point

Both from the repository root, offline. There is no registry: `npm install` has nothing
to fetch, and the pinned `tsc` **5.9.3** is vendored into the image as a global. The
runtime is node **24.18.1**, and it is the same runtime the original uses — so nothing
about number formatting, string handling or `RegExp` semantics changes under you.

Four properties of the built package are graded directly:

* **`dist/` is the delivered surface: copied anywhere on its own, it still works.** No
  reaching back into `src/`, no path relative to the repository root.
* **The library has to work when it is required directly, with no probe involved** —
  `require('./dist/jsonata.js')` gives the callable.
* **Requiring the package directory has to give the library**, which is what
  `package.json`'s `main` is for.
* **Two builds of the same tree must emit byte-identical output.** A build that stamps a
  timestamp or a hostname into `dist/` fails this.

The grading stage deletes `dist/` from its own copy before running your build, so
whatever you leave behind is not what gets graded. Build from source or not at all.

---

## How the result is judged

Three stages, in order. Each can end the run.

**1 — Audit review.** A model reads the original against your submission and answers
twelve questions, ten of which are required. It cannot build or run anything; it reads
code. Failing a required one is a **zero**, and nothing after it runs. What it is
looking for is the difference between a port and a rename: whether the delivered `src/`
is TypeScript you wrote, whether the strictness flags mean anything, whether the
declarations were generated, whether any third-party or original `jsonata` is reachable
at run time, and whether the tree is aware of the grader.

**2 — Behavioural tests, 40 points, or none.** Fifteen modules over 13,940 frozen cases,
compared byte-for-byte against answers captured from the original. No single module
vetoes the stage — the stage as a whole does: the 40 points are paid only for a
submission that answered every scored case, and the modules exist so that a report can
tell you which one you are short in. One module, `build`, carries no weight at all — it
is the step that produces the artefact the other fourteen measure, so a build that does
not work leaves them nothing to read and they fail on their own evidence. Short of every
scored case the ladder stops here with nothing, and stage 3 is never asked.

**3 — Verification, 60 points.** Six independent adversaries get an hour each, with your
source, the original's source, and both builds in front of them. Each tries to construct
one request the original answers and your port does not. Every adversary that finds
nothing is 10 points.

Stage 2 measures behaviour and is forbidden from asserting anything about how your
source looks. Stage 1 reads source and is given no toolchain. That division is
deliberate: **behaviourally, a rename of the original passes stage 2**, which is exactly
why "was this rewritten" is asked by readers and not by tests.

---

## Practical notes

* **The original is your oracle. Use it.** `npm run build && node dist/probe.js` works
  right now, offline. Before porting a builtin, ask the original what it returns for the
  input you are unsure about. `--selfcheck` reaches every op once.
* **Port `src/probe-wire.js` first and test it hardest.** It decides bytes. An encoder
  that is subtly wrong makes every other module look broken and tells you nothing about
  where the fault is.
* **Do the signature validator and the datetime formatter early.** They are the two
  modules where the letter-by-letter behaviour is least guessable and the diagnostics
  are most specific.
* **Type the evaluation focus deliberately.** `this.input` and
  `this.environment.lookup` inside a registered function are graded, and under
  `noImplicitAny` they are where a port either models the focus or gives up and casts.
* **A floor, not a target:** a delivered `src/` under **2,600** TypeScript logic lines
  is treated as evidence the port is not there. The original is 6,376 logic lines of
  JavaScript; a genuine port lands well above the floor without trying.
* Diagnostics on stderr are free — they are never compared. Use them.

`/opt/swerefactor/source-contract.json` states the mechanical parts of this document in
machine-readable form: the figures, the required tsconfig options, the fixed paths, the
forbidden patterns, the protocol codes, and the quirks listed above. Where the two
disagree, that file is what the graders read.
