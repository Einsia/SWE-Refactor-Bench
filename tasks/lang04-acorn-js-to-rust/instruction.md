# Port acorn from JavaScript to Rust

`/workspace/repo` is the acorn monorepo at version 8.14.0 — the ECMAScript parser
that webpack, rollup, ESLint and most of the JavaScript toolchain are built on.
It contains three published packages, all written in JavaScript:

| package | version | what it is |
| --- | --- | --- |
| `acorn` | 8.14.0 | the parser and tokenizer, plus the `acorn` CLI |
| `acorn-loose` | 8.4.0 | the error-tolerant parser |
| `acorn-walk` | 8.3.4 | the ESTree AST walker |

The technical debt is the implementation language. A JavaScript parser written in
JavaScript cannot be used from a native toolchain, cannot be linked into a
compiled program, and pays interpreter startup on every invocation. Your job is
to replace the JavaScript implementation with a Rust one, in this repository,
without changing what the software does.

Nothing about the *behavior* is up for negotiation. The parser you deliver must
produce the same ASTs, the same tokens, the same error messages, the same source
positions and the same JSON bytes as the JavaScript it replaces.

## Hard requirements

The repository must end up as a Cargo workspace with four members:

| crate | version | purpose |
| --- | --- | --- |
| `acorn` | 8.14.0 | library: parser and tokenizer |
| `acorn-loose` | 8.4.0 | library: error-tolerant parser |
| `acorn-walk` | 8.3.4 | library: AST walker |
| `acorn-cli` | 8.14.0 | binaries: `acorn` and `acorn-probe` |

`acorn-loose` and `acorn-walk` depend on `acorn`, as they do today. Crate
versions are read from `cargo metadata` and must match the table.

### Build

A `Makefile` at the repository root must provide:

```
make build                  # cargo build --release --offline
make install PREFIX=<dir>   # install the artifacts below under <dir>
```

`make build` must succeed with no network, an empty Cargo registry cache, and
`CARGO_NET_OFFLINE=true`. It must produce:

```
target/release/acorn
target/release/acorn-probe
```

Both must be native ELF executables produced by the pinned Rust toolchain
(1.90.0). Rust edition 2024 or lower. Both must run from a bare `PATH`, still run
when copied away from the build tree with nothing of it in the environment, hold
no reference to that tree, and need no shared library beyond libc.
`acorn-probe`'s `version` op reports `8.14.0`.

The two targets have to behave the way a distribution's build does:

- a second `make build` with nothing changed compiles nothing;
- `make install` works on a tree that was never built, and installing to a
  second `PREFIX` produces the same tree again;
- `make clean`, if you provide one, removes what the build produced and leaves
  the source alone;
- the installed documents are byte-identical to the repository's copies.

`make install PREFIX=<dir>` must produce exactly the six declared paths, with the
modes shown and nothing besides:

```
<dir>/bin/acorn                              0755, native executable
<dir>/bin/acorn-probe                        0755, native executable
<dir>/share/doc/acorn/README.md              0644
<dir>/share/licenses/acorn/LICENSE           0644
<dir>/share/licenses/acorn-loose/LICENSE     0644
<dir>/share/licenses/acorn-walk/LICENSE      0644
```

### Dependencies

`std`, `core` and `alloc`. Nothing else. No crates.io dependencies, no git
dependencies, no vendored crates, no build script that generates or downloads
code from outside the repository. Workspace members may depend on each other.

There is no Cargo registry in the image and no network, so this is not an honour
system — it is what will build.

Commit `Cargo.lock`. It must resolve to workspace members only.

The Unicode tables in `acorn/src/generated/` and
`acorn/src/unicode-property-data.js` are data, not dependencies. Port them.

### What must be gone

Every one of these leaves the repository:

- `*.js`, `*.mjs`, `*.cjs`, `*.jsx` — the whole JavaScript implementation,
  including `test/` and `test/bench/fixtures/`
- `*.ts`, `*.mts`, `*.cts`, `*.tsx` — the TypeScript declarations
- `package.json`, `package-lock.json`, `.npmrc`, `.npmignore`
- `.eslintrc.js`, `.eslintignore`, `.tern-project`
- any `node_modules` directory
- `*.map`, `*.node`, `*.wasm`

Renaming a JavaScript file to `.txt`, moving it into a data directory, or
embedding its text in a Rust string literal does not count as removing it. The
requirement is that State A's implementation is not in this repository in any
form — not that its filenames are gone.

### How much Rust

At least **4,000** non-blank, non-comment lines of Rust under the repository.
State A's three `src` trees are 6,549 such lines of JavaScript, and a parser,
tokenizer, regexp validator, error-tolerant parser and walker do not come out
substantially shorter in Rust.

This is a condition on the deliverable, not something behaviour can buy back.
Deleting the JavaScript and shipping a stub that answers a few operations
correctly does not satisfy it, however well the stub does on the operations it
answers.

### What must stay

```
README.md
AUTHORS
acorn/LICENSE            acorn/CHANGELOG.md
acorn-loose/LICENSE      acorn-loose/CHANGELOG.md
acorn-walk/LICENSE       acorn-walk/CHANGELOG.md
```

Update `README.md` to describe the Rust build. Do not rewrite the licences or
the changelogs, and do not add a new version entry: this is a port, not a
release.

### State A's toolchain

State A's npm dependencies are installed at `/workspace/node_modules`, outside
the repository. `npm run build` and `node test/run.js` work today and are there
for you to study the reference with — 6,763 upstream tests, all passing.

Use them as much as you like. They are not part of the submission, and nothing
you deliver may reach for them: neither the build nor the finished binaries may
invoke `node` or any other interpreter. Assume that whatever a JavaScript
runtime does for you today, it will not do at grading time.

## The `acorn` CLI

`target/release/acorn` replaces `acorn/bin/acorn` exactly: same flags, same
stdout bytes, same stderr bytes, same exit codes.

```
usage: acorn [--ecma3|--ecma5|--ecma6|--ecma7|--ecma8|--ecma9|...|--ecma2015|--ecma2016|--ecma2017|--ecma2018|...]
        [--tokenize] [--locations] [--allow-hash-bang] [--allow-await-outside-function] [--compact] [--silent] [--module] [--help] [--] [<infile>...]
```

Behaviour that is part of the contract, all of it observable by running State A:

- `--help` prints usage to stdout and exits 0. An unrecognised flag prints the
  same usage to **stderr** and exits 1.
- With no file arguments, source is read from stdin. `-` means stdin. After
  `--`, every remaining argument is a filename even if it starts with `-`.
- Output is `JSON.stringify(result, null, 2)`, or `JSON.stringify(result)` under
  `--compact`, followed by a newline. `--silent` prints nothing.
- On a parse error, the error message goes to stderr and the exit code is 1. In
  file mode the message has the filename spliced into its `(line:column)`
  suffix.
- When several files are given, each is parsed in turn with the previous result
  passed as `options.program` — so the final AST accumulates all of them.
- When `ecmaVersion` is not given, State A prints a two-line warning to stderr
  and parses as 2020. Reproduce the warning, on stderr, byte for byte.
- `--ecma1` and `--ecma99999` are accepted: anything matching `/^--ecma(\d+)$/`
  becomes `ecmaVersion`. `--ecma` with no digits does not match, so it prints
  usage and exits 1. `--ecma5 --module` parses — `sourceType` is independent of
  the version.

`usage:` uses `basename(process.argv[1])`, which is `acorn`. Keep it `acorn`.

### Where State A dies instead of reporting

Five argv shapes make State A fail without printing an acorn message, because an
exception escapes the CLI and node prints its own stack trace. That trace names
absolute paths inside node's installation and acorn's `dist/`, so it is not
something a Rust program can reproduce and you are not asked to.

For these five, what is graded is: **the exit status, that stdout stays empty,
and that something is written to stderr.** The text on stderr is yours to
choose.

- a file that does not exist, a directory, or the empty string as a filename —
  `readFileSync` throws before parsing begins
- `-` together with another argument: `-` is then an ordinary filename, and
  there is no file called `-`
- a source containing a BigInt literal, such as `let big = 10n`. The parse
  succeeds; `JSON.stringify` then throws `TypeError: Do not know how to
  serialize a BigInt`. So a faithful port also fails here, after parsing, having
  printed no tree. It must not print a tree with the BigInt serialised somehow.

Every other CLI case is graded on exact stdout bytes, exact stderr bytes and the
exit status.

## The probe protocol

Rewriting a library replaces its API, and a Rust API cannot be called the way a
JavaScript one was. So the library surface is graded through a second binary,
`acorn-probe`, which speaks a line protocol. This is part of the deliverable, not
a test harness: it is how anything outside the process reaches the library.

`acorn-probe` reads NDJSON on stdin — one JSON object per line — and writes one
NDJSON response per line to stdout, in request order, flushing each line. On EOF
it exits 0. It must never exit early, however malformed a request is.

A request is:

```json
{"id": 1, "op": "parse", "source": "let x = 1", "options": {"ecmaVersion": 2020}}
```

A response is minified JSON with keys in exactly this order:

```json
{"id":1,"ok":true,"result":<value>}
{"id":1,"ok":false,"error":{"kind":"SyntaxError","message":"Unexpected token (1:8)","pos":8,"raisedAt":9,"loc":{"line":1,"column":8}}}
```

The error line above is the real response to `{"id":1,"op":"parse","source":"let x = }",`
`"options":{"ecmaVersion":2020}}`. Note that `pos` and `raisedAt` are different
numbers: `pos` is where the offending token starts, `raisedAt` is where the
tokenizer had got to. They coincide often enough that returning one for the other
passes a careless test and fails this one.

`ok` is `false` only when the corresponding JavaScript call throws. `kind` is the
JavaScript error constructor name. For a `SyntaxError` raised by the parser,
`message`, `pos`, `raisedAt` and `loc` are the properties acorn sets on it. For
any other throw, `kind` and `message` alone.

A few dozen expectations in the frozen corpus are a `TypeError` thrown from inside
acorn's own JavaScript rather than an error acorn raises on purpose. Their
`message` is the JavaScript engine's phrasing for the shape of the code that threw,
not anything acorn defines — the same acorn-walk source reports `node.attributes is
not iterable` when the engine reads it as ES2020 and `Cannot read properties of
undefined (reading 'length')` through the ES5 bundle. Those cases are not asked of
your port. The verifier skips
each one with a reason in the transcript and rates the module over what it did
ask, so neither reproducing them nor handling the input sanely changes your score.
Every other error in the corpus is one acorn constructs itself, and those are
yours to reproduce exactly.

### Operations

| op | fields | result |
| --- | --- | --- |
| `version` | — | `acorn.version` |
| `default_options` | — | `acorn.defaultOptions` |
| `token_types` | — | object mapping every `acorn.tokTypes` key to its token type |
| `keyword_types` | — | same for `acorn.keywordTypes` |
| `parse` | `source`, `options` | `acorn.parse(source, options)` |
| `parse_collect` | `source`, `options` | `{"ast":…,"comments":…,"tokens":…}` — `parse` with `onComment` and `onToken` bound to arrays, which the result reports in visit order |
| `parse_expression_at` | `source`, `pos`, `options` | `acorn.parseExpressionAt(source, pos, options)` |
| `tokenize` | `source`, `options` | array of every token from `acorn.tokenizer(...).getToken()` up to and including `eof` |
| `loose_parse` | `source`, `options` | `acornLoose.parse(source, options)` |
| `walk_full` | `source`, `options` | array of `[type, start, end]` in `walk.full` visit order |
| `walk_full_ancestor` | `source`, `options` | array of `[type, start, end, [ancestor types…]]` in `walk.fullAncestor` visit order |
| `walk_simple` | `source`, `options`, `visitors` | array of `[type, start, end]` for the node types named in `visitors`, in `walk.simple` visit order |
| `walk_recursive` | `source`, `options`, `stop_at` | `walk.recursive` where the node types in `stop_at` have a visitor that records the node and does **not** recurse; every other type falls through to `base`. Result is the recorded array |
| `find_node_at` | `source`, `options`, `start`, `end`, `test` | `walk.findNodeAt`; `start`/`end` may be `null`, `test` is a node type name or `null` |
| `find_node_around` | `source`, `options`, `pos`, `test` | `walk.findNodeAround` |
| `find_node_after` | `source`, `options`, `pos`, `test` | `walk.findNodeAfter` |
| `find_node_before` | `source`, `options`, `pos`, `test` | `walk.findNodeBefore` |
| `get_line_info` | `source`, `offset` | `acorn.getLineInfo(source, offset)` |
| `is_identifier_start` | `code`, `astral` | `acorn.isIdentifierStart(code, astral)` |
| `is_identifier_char` | `code`, `astral` | `acorn.isIdentifierChar(code, astral)` |
| `is_new_line` | `code` | `acorn.isNewLine(code)` |
| `line_break_test` | `text` | `acorn.lineBreak.test(text)` |
| `nonascii_whitespace_test` | `text` | `acorn.nonASCIIwhitespace.test(text)` |

`options` is always the options object acorn would receive, and may be absent.
`find_node_*` return `undefined` when nothing matches, which JSON omits — so the
response is `{"id":N,"ok":true}` with no `result` key. That is the correct
answer, not a failure.

### Encoding

Results are the JSON your JavaScript counterpart would produce, byte for byte.
That is a stronger requirement than it looks:

- **Property order is insertion order.** `JSON.stringify` emits keys in the order
  the object acquired them. An acorn node acquires `type`, `start`, `end`, then
  `loc`/`range`/`sourceFile` if those options are on, then the node's own fields
  as the parser fills them in, and `type` is rewritten in place by `finishNode`.
  Get the order wrong and the bytes differ.
- **Numbers use ECMAScript `Number::toString`.** `1e21` is `1e+21`, `1e-7` is
  `1e-7`, `-0` is `0`, `1/3` is `0.3333333333333333`.
- **A function-valued property is omitted; a `null` one is not.** Every
  `TokenType` has an `updateContext` field. For most types it is `null` and
  appears in the JSON. For the ones `tokencontext.js` assigns a function to, the
  key is absent entirely.
- **`undefined` properties are omitted.** `TokenType.keyword` is `undefined`
  unless the type is a keyword.
- **A regular-expression literal's `value` serialises as `{}`**, and is `null`
  when the pattern cannot be compiled.
- **BigInt cannot be serialised.** Where a value is a BigInt — a `Literal` node's
  `value` for `10n` — emit `{"$bigint":"<decimal digits>"}` instead. This is the
  one place the probe deviates from `JSON.stringify`, which throws there. The
  `bigint` property, which is a string, is unaffected.

Strings are escaped as `JSON.stringify` escapes them: `"`, `\`, the C0 controls,
and lone surrogates as `\udXXX`. Nothing else — no `U+2028`/`U+2029` escaping, no
non-ASCII escaping.

## Behaviour: byte-exact, against the JavaScript implementation

Compatibility is defined against acorn 8.14.0 as it actually behaves, not
against the ESTree specification and not against what a JavaScript parser ought
to do. Where the implementation has a quirk, reproduce the quirk.

Some you will find; these are free:

- `parse("a", {})` with no `ecmaVersion` warns on stderr and behaves as 2020.
- A `Literal` for a regexp gets `value`, `raw`, then `regex` — in that order.
- `acorn-loose` invents nodes with the name `✖` for missing identifiers.
- `getLineInfo` counts `\r\n`, `U+2028` and `U+2029` as line breaks; `tabSize`
  does not affect it.
- The CLI's `--tokenize` output includes the whole `TokenType` object per token.

The verifier compares your bytes with the JavaScript implementation's bytes on
thousands of inputs, including the six real-world bundles in
`test/bench/fixtures/` — jQuery, React, React-DOM, Backbone, Angular and Ember,
between 72 KB and 1.8 MB each.

Most of those inputs come from sources you have: the snippets upstream's own test
files feed their own parser. That is deliberate — they are the cases acorn's
authors thought worth writing down, which makes them the cases a port breaks on.
What you do not have is the case list. Which snippets are used, in which of the
option combinations, under which of the twenty-three operations, and about a
seventh of the inputs generated outright, all follow from a seed that exists only
inside the verifier image. So there is no set to tune to and no table to
precompute; the only thing that generalises is a parser that behaves like the
one in front of you. You can always find out what the right answer is — build
State A and ask it, which is what the verifier did.

Passing `test/run.js` is necessary, and a long way from sufficient.

## What done means

Three questions, asked in that order, and the first one is a condition rather
than a quantity.

**Was the debt actually paid?** The JavaScript implementation is out of the
repository; the parser here is this repository's own; nothing executes JavaScript
at build time or run time; the default `make build` path reaches exactly one
parser; no copy of acorn is shipped or fetched; the ASTs are computed from the
input rather than recalled from a table. A submission that keeps the JavaScript
and wraps it reproduces acorn's behaviour perfectly by doing none of the work,
which is why this is asked first and why nothing later is worth anything if it
does not hold.

**Does it still do what it did?** Every public surface, differential against the
JavaScript implementation: `parse` and its options, the tokenizer, the loose
parser, the walker, the position queries, the character-class and line
utilities, and the CLI. There is no partial credit here. A parser that handles ES5
correctly and nothing else earns what one that handles nothing earns; getting
`parse` right and `tokenize` wrong costs you the stage, not `tokenize`. The report
is granular so you can see where you stand — the payment is not. Work outward from
the core, and get to the edge.

**Does it hold up under someone looking for the seam?** Independent readers get
both trees and both builds and try to construct an input the JavaScript parser
handles and yours does not. What earns credit here is the absence of such an
input, so the cases to worry about are the ones no test list would have thought
to include.

A helper crate of your own, extra `make` targets, and any wording you like in the
README are all fine.

## Practical notes

- `git log` has exactly one commit, and it is the starting state. The upstream
  history is not available in this repository — the port is the work, not
  finding it.
- There is no network, at build time or at run time.
- `cargo build` output goes to `target/`, which is discarded before grading.
  Do not put anything there you need.
- A submission that shells out to `node`, `npm`, `deno`, `bun` or any other
  JavaScript runtime does not build at grading time and does not run there.
  Do not treat the presence of a name on `PATH` as permission to call it.
- Both binaries are run with a working directory that is not the repository, so
  do not resolve anything relative to `.`.
- `acorn-probe` handles requests one line at a time and must not buffer its
  output: the verifier writes a request and waits for the response line.
