# Port Stylus off Node.js and onto the Web platform

You are working in `/workspace/repo`. It contains Stylus 0.63.0 exactly as
released: a CSS preprocessor of about 16,000 lines across 137 modules in `lib/`,
an 846-line CLI in `bin/stylus`, and the upstream test suite in `test/`.
Dependencies are already installed. There is no network.

Stylus today only runs on Node.js. Not because of anything about CSS, but
because Node is woven through the compiler: `require` graphs, `fs.readFileSync`
called from inside the evaluator's recursive descent, `Buffer` in the image
header parser, `crypto.createHash` in the cache, `node:path` imported by sixteen
modules, `__dirname` to find its own built-in `.styl` library. It cannot run in
a browser, in a service worker, in a Cloudflare Worker, in Deno without a
compatibility shim, or in any other environment that implements web standards
rather than Node's API.

Your job is to move it. When you are finished, the compiler must run on a
runtime that provides only standard web globals, while continuing to work
exactly as it does today for existing Node users.

`git log` shows one commit, tagged `state-a`. That is where you started; diff
against it whenever you want to see your own footprint.

You also get State A as something you can *run*, which matters because §2.1 has
you delete `lib/` — the implementation you are being compared against would
otherwise vanish from the container on your first change:

- `/opt/state-a` — a read-only, still-installed copy of the tree as you found
  it, also at `$STYLUS_STATE_A_ROOT`
- `stylus-state-a` — on `PATH`, same arguments as `bin/stylus`, so
  `diff <(stylus-state-a x.styl) <(./bin/stylus x.styl)` works

Use it as the oracle. Every question of the form "what does Stylus do with
this?" has an answer you can obtain directly rather than infer, and that answer
is the one you are scored against. Nothing in the container reports on your
score.

---

## 1. What must exist when you are done

### 1.1 `src/core/` — the platform-neutral compiler

Every module under `src/core/` must be an ES module that runs with **no Node
API whatsoever**. Concretely, `src/core/` must not, anywhere:

- `import` or `require` `node:*` or any bare builtin (`fs`, `path`, `url`,
  `crypto`, `buffer`, `events`, `util`, `os`, `process`, `module`, …)
- `import` a bare package specifier of any kind — every import inside
  `src/core/` is relative, beginning `./` or `../`, **and resolves to a file
  inside `src/core/`**. The directory is closed under import: the verifier
  loads it as a self-contained unit and refuses a specifier that resolves
  outside it, so a helper you want the core to use belongs under `src/core/`
  even when it is pure computation with no host dependency
- reference `process`, `Buffer`, `__dirname`, `__filename`, `require`,
  `module`, `exports`, or `global`
- use `import()` with a computed specifier

The verifier loads `src/core/index.js` into a fresh V8 realm whose global object
was created empty and then populated with exactly this set and nothing else:

```
globalThis  Object  Array  Function  String  Number  Boolean  Symbol  BigInt
Math  JSON  Date  RegExp  Error  TypeError  RangeError  SyntaxError
ReferenceError  EvalError  URIError  AggregateError  Map  Set  WeakMap  WeakSet
Promise  Proxy  Reflect  ArrayBuffer  SharedArrayBuffer  DataView  Uint8Array
Int8Array  Uint8ClampedArray  Int16Array  Uint16Array  Int32Array  Uint32Array
Float32Array  Float64Array  BigInt64Array  BigUint64Array  Atomics
WeakRef  FinalizationRegistry  Iterator  Intl
TextEncoder  TextDecoder  URLSearchParams  crypto (SubtleCrypto + getRandomValues)
btoa  atob  structuredClone  queueMicrotask  console
parseInt  parseFloat  isNaN  isFinite  encodeURIComponent  decodeURIComponent
encodeURI  decodeURI  escape  unescape  undefined  NaN  Infinity  eval
```

Note what is absent: no `fetch`, no `URL`, no `setTimeout`, no `WebAssembly`, no
`performance`. Do not rely on them. `crypto.subtle.digest` is available and is
**asynchronous** — the only hashing primitive you get.

A few names beginning `__` also exist in the realm. They belong to the harness —
one of them is a synchronous hash, which is not a primitive this platform gives
you. Reading or calling one is an audit failure under §4.

`src/core/index.js` must have a default export carrying at least:

| name | meaning |
|---|---|
| `version` | the string `"0.63.0"` |
| `stylus(str, options)` | also the callable default export itself; returns a `Renderer` |
| `render(str, options)` | convenience wrapper |
| `Renderer` | class |
| `Parser`, `Lexer`, `Evaluator`, `Compiler`, `Normalizer`, `DepsResolver` | classes |
| `nodes`, `utils`, `functions` | the same objects State A exposes |
| `convertCSS(css)` | the CSS→Stylus converter |
| `resolver(options)`, `url(options)` | the built-in `url()` implementations |
| `path` | the POSIX path algebra of §1.2, so a host can share it |

### 1.2 The capability object

`src/core/` reaches the outside world through one object and no other channel.
It arrives as `options.platform` on `stylus(str, options)` / `render(str, options)`:

```js
platform = {
  sync: Boolean,                  // true if the four calls below return values
                                  // directly instead of promises

  readFile(path)  -> Uint8Array           // rejects/throws if absent
  readDir(path)   -> string[]             // entry names, not paths; unordered
  stat(path)      -> { isFile: Boolean, isDirectory: Boolean, size: Number }
                                          // null (not a throw) when absent
  sha1(bytes)     -> string               // 40 lowercase hex chars

  cwd: String,                    // POSIX absolute, no trailing slash
  runtimeRoot: String             // POSIX absolute directory holding the
                                  // built-in .styl library shipped in 1.4
}
```

All paths crossing this boundary are POSIX: `/` separators, no drive letters.
`readFile` returns bytes, never a string — decoding is the compiler's job, and
it must decode UTF-8 and honour a leading BOM the way State A does.

`src/core/` must contain its own POSIX path algebra, exported as `path` on the
default export. `join`, `resolve`, `dirname`, `basename` (including the
two-argument `basename(p, ext)` form), `extname`, `relative`, `normalize` and
`isAbsolute` must agree with `node:path.posix` on every input the verifier
tries, including the awkward ones: `''`, `'.'`, `'..'`, `'/'`, `'//a//b'`,
`'a/b/../..'`, `'../../x'`, trailing slashes, and `extname('.hidden')`.

`resolve` is given an absolute first segment in every vector the verifier uses,
so it never has to consult `platform.cwd`.

### 1.3 Two render paths

```js
renderer.render()      -> Promise<String>   // always available
renderer.renderSync()  -> String            // requires platform.sync === true
```

`renderSync()` must throw a clear `Error` when given an async platform. Both
paths must produce byte-identical CSS for identical input. This is the crux of
the port: the evaluator resolves `@import` from inside its recursive descent,
so the IO cannot simply be hoisted out. How you make one compiler serve both
modes is your decision — driving IO through generators that the two runners
consume differently, and pre-resolving the import closure before evaluation,
are both workable. Duplicating the evaluator is not: see §2.4.

### 1.4 Built-in `.styl` library

State A finds `lib/functions/index.styl` with `__dirname`. `src/core/` has no
`__dirname`. The built-in library must be loaded through
`platform.readFile` under `platform.runtimeRoot`, so a web host can supply it
from wherever it likes. Ship the `.styl` sources under `src/core/` and have the
Node adapter point `runtimeRoot` at them.

### 1.5 `src/node/` — the Node adapter

`src/node/index.js` is what existing Node users get. It implements `platform`
with `sync: true` on top of `node:fs`, `node:crypto` and `node:path.posix`, and
re-exports the compiler with State A's behaviour intact — **including a
synchronous `render()`**:

```js
import stylus from 'stylus';
stylus('a\n  color red\n').render();     // returns a String, not a Promise
```

Everything State A's JS API does must keep working through this adapter, with
the same results: `.set()`, `.get()`, `.define()` (including the raw-object and
hash forms), `.include()`, `.import()`, `.use()`, `.deps()`, `.render(cb)` with
a callback, `options.globals`, `options.functions`, `options.imports`,
`options.paths`, `options.filename`, `options.compress`, `options.sourcemap`,
`options.hoist atrules`, `options.include css`, `options.prefix`,
`options.resolve url`, `stylus.middleware`, `stylus.convertCSS`,
`stylus.resolver`, `stylus.url`.

Error objects must keep their identity and their message text: a
`ParseError`/`SyntaxError` for `a\n  color: ` must carry the same `.name`,
the same first line of `.message`, and the same `.lineno`, `.column` and
`.filename` as State A produces.

`package.json` must expose both faces:

```json
"exports": {
  ".":     "./src/node/index.js",
  "./web": "./src/core/index.js"
}
```

### 1.6 `bin/stylus`

Still there, still executable, still `#!/usr/bin/env node`, and now a client of
`src/node/`. Every flag State A accepts keeps its meaning and its output:
`-c/--compress`, `-o/--out`, `-I/--include`, `-w/--watch`, `-U/--inline`,
`-m/--sourcemap`, `--sourcemap-inline`, `--sourcemap-root`, `--sourcemap-base`,
`-p/--print`, `-i/--interactive`, `--resolve-url`, `--include-css`,
`--hoist-atrules`, `--prefix`, `--css` (CSS→Stylus conversion), `-V/--version`,
`-h/--help`, stdin→stdout with no arguments, and exit status 1 with the error
on stderr for a compile failure.

---

## 2. What must no longer exist

### 2.1 `lib/` is gone

Not renamed, not re-exported, not kept "for compatibility". The directory
`lib/` must not exist in the submitted tree, and no file outside `test/` may
import from it.

### 2.2 No Node API reachable from the core

`src/core/` must not import a Node builtin — see §1.1 for the full list. This
is checked both statically over your sources and dynamically: the verifier
watches whether a compile ever reaches outside the injected capability object.

### 2.3 No shim layer

A module that re-implements `fs`, `path`, `Buffer`, `process` or `require`
inside `src/core/` and then hands it to otherwise-unchanged State A code is not
a port. Specifically, `src/core/` must not contain a module whose default
export is an object with a `readFileSync` method, must not define a global
called `process` or `Buffer`, and must not synthesise `__dirname` from a
string constant.

Reimplementing POSIX path arithmetic (§1.2) is required and is not a shim: it
is pure computation with no host dependency. The line is host access — bytes,
directories, hashes, clocks — which must go through `platform`.

### 2.4 No second compiler

There must be exactly one implementation of each compiler stage. The verifier
compares the two render paths of §1.3 for structural identity: if `renderSync`
and `render` reach different evaluator sources, or if two files under `src/`
contain near-duplicate copies of the same visitor, that is a failed migration
even when both produce correct CSS.

### 2.5 No test-shaped shortcuts

The verifier's tests are not in this repository and you cannot see them. Code
that behaves differently depending on whether it thinks it is being tested —
inspecting env vars, filenames, stack traces, or the shape of the platform
object to pick a code path — fails the audit gate. Precomputed output
tables keyed by input hash likewise.

---

## 3. What must not change

- **`test/`** — leave the upstream suite and its 356-case corpus alone. Read
  it, run it, but do not edit a `.styl`, a `.css`, a `.map` or a `.deps` file
  in it, and do not weaken an assertion. The verifier scores against its own
  pristine copy of these inputs, so editing them gains nothing and is
  detected. You may add a *new* test file of your own.
- **CSS output** — byte for byte, for every input, under every option
  combination. Including whitespace, including `#f00` versus `red`, including
  the order of hoisted at-rules, including compressed output.
- **`package.json`** identity: `name`, `version`, `license`, `bin`, and the
  five runtime dependencies stay as they are. You may add `type`, `exports`,
  `files`, `engines`, and scripts.
- **The five dependencies** — `@adobe/css-tools`, `debug`, `glob`, `sax`,
  `source-map` — stay in `package.json` and stay available to `src/node/`. You
  may not add a sixth: there is no network, and a new dependency would not
  install. Note that `glob`, `sax` and `source-map` are Node packages, so
  whatever `src/core/` needs from them it must do itself.
- **`Readme.md`, `LICENSE`, `Changelog.md`** and the other top-level documents.

---

## 4. How you are graded

Three stages, in order. A stage that stops the ladder means the later ones do
not run.

### Stage 1 — migration audit (pass/fail)

A reviewer is given State A and your tree side by side, reads both, and answers
one question in several forms: is this a port, and is the ported code what
actually runs? It reads; it does not execute your code, so nothing here depends
on a test you could satisfy narrowly.

Failing it scores the whole submission zero, however good the CSS is. What
fails it is §1 through §3 not being true of your tree — the old implementation
still present or still reachable, the core still touching a host facility, a
shim standing in for the migration, two compilers where there should be one,
frozen inputs edited, or code that behaves differently when it suspects it is
being graded. There is no checklist to satisfy here beyond the requirements
already stated: a reviewer with both trees open is asking whether you did the
thing, not whether a pattern appears.

### Stage 2 — behavioural parity (40 points, or none)

Run only if stage 1 passes. Your tree is copied out, its dependencies installed
offline from its own lockfile, and both faces are exercised: the core loaded into
the §1.1 realm, the Node adapter imported as an ordinary package, and `bin/stylus`
run as a program. Expectations are computed at verify time by running pristine
State A on the same inputs, in a container you never see, so there is nothing to
precompute against.

Nothing in this stage looks at your source. It compares what your build does
with what State A's does — CSS bytes, return values, error fields, exit codes,
stdout and stderr, sourcemap contents, the sequence of reads your platform
object receives. How you got there is stage 1's business, not this stage's.

The stage is reported across a dozen areas — the corpus through each face, the
option matrix, the built-in functions, path algebra, import resolution, the CLI,
the JS API, diagnostics, sourcemaps — so a report tells you where you are thin.
The 40 points are not divided that way. They are paid only for a submission that
passed every scored check in every area, so a submission that ports the compiler
but never gets `renderSync` working is paid what one that ported nothing is paid.

Two things constrain that, stated so you do not waste effort on them:

- **The denominators are fixed in advance.** Each area is measured against the
  number of checks it is *supposed* to contain, sealed when this task was built.
  A check that errors, crashes the runner, or never gets collected counts exactly
  as a check that failed. There is no version of "make the test not run" that
  scores better than "make the test pass".
- **Staging is required.** If your tree cannot be copied and installed from its
  own lockfile, offline, with the cache you were given, then nothing downstream
  can run and the stage scores zero. Do not add a dependency, and do not leave
  the lockfile describing something other than what you ship.

### Stage 3 — verification search (60 points)

Run only if stage 2 came in at full marks — every scored check passed. Anything
short of that and the ladder stops there, stage 2 pays nothing, and this stage is
never reached. If stage 2 already found a place where your port disagrees with
State A, six models hunting for another one tells you nothing the behavioural report
has not.

Six models are run independently. Each is given State A and your tree, both
installed from their own lockfiles and reachable the way a real consumer reaches
the package. Each reads your code — looking for the place where the port is thin
— and then writes an input designed to make the two disagree: a stylesheet, an
options object, a sequence of API calls, a command line. The input is run against
both, more than once. If the outputs differ, and differ the same way every time,
that model found a break.

You get **10 points for every model that finds nothing**, so a submission no
model can break scores the full 60.

This is the difference between passing the tests you imagined and having
actually ported the compiler. Stage 2 asks whether you match State A on the
inputs the task chose; stage 3 lets something read your implementation and pick
the input itself. The defence is the same either way: no case where your version
does something State A would not.

Differences that follow necessarily from doing the port as specified are not
held against you. A break has to be a disagreement about compiling
stylesheets — one that a State A user would notice.

---

## 5. Ground rules

- Edit `/workspace/repo` in place. Do not produce a patch file; the directory
  itself is collected.
- No network. Everything you need is installed.
- `node_modules/` is discarded before scoring and reinstalled from the lockfile,
  so do not edit anything inside it and do not vendor code into it.
- Commit or don't; only the working tree is read.
- The verifier runs `node` 22 with `--experimental-vm-modules`. You may assume
  ES2023.
- Budget your time. Getting the 356-case corpus through the sandboxed core is
  worth more than polishing any single flag.
