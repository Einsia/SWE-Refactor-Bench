# Port go-yaml from Go to Zig

`/workspace/repo` is `gopkg.in/yaml.v3` v3.0.1: a complete YAML 1.1/1.2
implementation in 11,285 lines of Go across thirteen non-test files, descended
from libyaml and hand-translated into Go. A scanner that produces tokens, a parser
that turns them into events, a resolver that decides what `yes` means, a node
builder that assembles `yaml.Node` trees with comments attached, and an emitter
whose line breaking, indentation and quoting decisions are all its own. Six
`_test.go` files sit beside them; they are go-yaml's own tests, they are not part
of what you are porting, and they go with the Go.

Two directories beside the library are not upstream's: `probe/` and
`cmd/yaml-probe/`, 623 lines of Go that speak the NDJSON protocol below over the
library's public API. That is the working reference for the protocol — the same
adapter the grader's own reference binary is built from, so what it puts on the
wire is by definition correct — and `go build ./...` produces `bin/yaml-probe` from
it today. Read it: it answers every question about framing, field names and error
shape that the protocol section leaves to a worked example. It is also Go, so it
leaves with the rest.

Reimplement it in Zig, in place, in this same directory.

The port is judged by behaviour, not by shape. Nothing requires you to keep
go-yaml's file layout, its function names, or its state machines — if you can
produce the same trees and the same bytes with a different design, that scores
the same. What is fixed is the observable surface: the node tree, the emitted
text, the error strings, and the protocol below.

This document is the contract. `source-contract.json` in this directory is the
same contract in machine-readable form; where a number appears in both, that file
is authoritative. Nothing outside this document is graded, and everything inside
it is.

## Hard requirements

### Build

`zig build` at the repository root, with no arguments, must produce:

```
zig-out/bin/yaml-probe
```

a native ELF executable built by the pinned toolchain, **Zig 0.14.1**. It must
succeed offline, with an empty package cache, and with `ZIG_GLOBAL_CACHE_DIR`
pointing somewhere outside the repository.

`zig build test` must exist as a step. It is not scored, and it may run zero
tests, but the step has to be there — the grader invokes it and a missing step is
a build error, not a skipped one.

Keep `build.zig` and `build.zig.zon` at the root, and `src/` as the source root.
Both files are stubs today and are yours to rewrite; the names and the output
path are what is fixed.

### Dependencies

The Zig standard library. Nothing else.

`.dependencies` in `build.zig.zon` must stay empty. No vendored Zig packages, no
`@cImport`, no `zig translate-c`, no C, C++ or Objective-C sources, no prebuilt
object files or archives, no linking against a system library. Every byte of
executable code in `yaml-probe` must come from the pinned Zig compiler reading
Zig sources inside your submission.

`@cImport` deserves its own sentence, because Zig makes it so easy: pulling in
libyaml, or `translate-c`-ing a C YAML parser, would satisfy every behavioural
test in this task and port nothing. It is checked for, and it fails the
submission.

Two more things that are not dependencies but are also not allowed: running any
Go toolchain binary at build time or run time, and embedding a table of expected
probe responses. The first is delegation; the second is memorisation.

### What must be gone

The Go implementation leaves the source closure:

- `*.go` — all seventeen implementation files and all seven `_test.go` files: the
  library's thirteen and six, and the protocol adapter's four and one
- `go.mod`, `go.sum`, `go.work`, `go.work.sum`, any `vendor/` directory
- `.golangci.yml`, `Makefile.go`
- compiled artefacts of any kind: `*.o`, `*.a`, `*.so`, `*.dylib`, `*.dll`,
  `*.obj`, `*.lib`, `*.wasm`
- C-family sources: `*.c`, `*.h`, `*.cc`, `*.cpp`, `*.hpp`, `*.m`

Renaming `scannerc.go` to `scannerc.go.txt`, moving it under `docs/`, or
embedding its text in a Zig string literal does not count as removing it. The
verifier looks for the original implementation by content, not by filename.

`zig-out/`, `.zig-cache/` and `zig-cache/` are build output. They are ignored by
this check and discarded before grading.

### What must stay

```
LICENSE        NOTICE        README.md
```

go-yaml is MIT and Apache-2.0; the notices travel with the port. Do not rewrite
them. Update `README.md` to describe the Zig build — that file is expected to
change, and it is the one place a human looking at your repository learns how to
build it.

### The floor

At least **4,000** non-blank, non-comment lines of Zig under the repository.

State A is 7,609 such lines of graded Go — every non-test source except
`sorter.go`, which orders map keys for the typed `Marshal` path this task does
not grade. The floor sits well below that on purpose: a good Zig port is shorter
than the Go in places — no `reflect`, no `interface{}` juggling — and longer in
others, and nobody should be padding to hit a number. It exists to reject a
200-line shim that delegates the work somewhere else.

### State A's toolchain

Go 1.23.4 is on `PATH` in the agent image, and `go build ./...` and
`go test ./...` work on the tree as delivered. All upstream tests pass. Use them
freely: the `_test.go` files are the most precise description of the behaviour
you have to reproduce, and reading them is expected.

They are not the grading expectations. Those come from running the original
library over inputs you have not seen. And `go` will not exist when your work is
graded — a submission that shells out to it fails.

## The probe protocol

`zig-out/bin/yaml-probe` is how your port is measured. It speaks NDJSON on
stdin/stdout: one JSON request per line in, one JSON response per line out, in
the order received, **flushed per line**.

The grader writes a request and then blocks reading the response. A probe that
buffers its output waiting for more input deadlocks instead of failing, and a
deadlock costs the whole run rather than one case. Flush after every line.

Empty lines are skipped, not answered. End of stdin means exit 0.

A request line is a JSON object:

| key | type | meaning |
| --- | --- | --- |
| `id` | integer | echoed back verbatim |
| `op` | string | one of the operations below |
| `source` | string | the YAML input |
| `indent` | integer, optional | only meaningful for `emit_indent` |

`note` may appear and must be ignored. Requests are yours to parse with anything
you like: they are generated input, never compared, so no rule constrains how you
read them. A line that is not valid JSON gets a `ProtocolError` response with
`id` 0.

A response line is a JSON object with keys in exactly this order:

```
{"id":<int>,"ok":true,"result":{...}}
{"id":<int>,"ok":false,"error":{"kind":<string>,"message":<string>}}
```

`kind` is one of:

| kind | when |
| --- | --- |
| `YamlError` | the library rejected the input, or emitting failed |
| `ProtocolError` | the request itself was malformed |
| `Panic` | an internal failure escaped — a defect, or a resource limit |

`Panic` exists so a crash cannot be mistaken for expected behaviour. Never
report a YAML error as a `Panic`, and never let a `Panic` case take down the
process: one bad case should cost one case.

### Operations

Six operations are graded. A seventh, `hello`, is a handshake.

**`hello`** — ignores `source`. Result:

```json
{"protocol":1,"library":"gopkg.in/yaml.v3","upstream":"v3.0.1"}
```

**`node`** — parse `source` as a single document, `yaml.Unmarshal` into a
`*yaml.Node`. Result `{"node":<node>}`. A parse failure is a `YamlError`.

**`stream`** — decode every document in `source` with a streaming decoder.
Result:

```json
{"docs":[<node>,...],"error":<string|null>}
```

A failure part way through is reported **alongside the documents already read**.
A stream that yields two documents and then fails is evidence about all three
outcomes; collapsing it to a bare error throws two thirds of that away. `docs`
is `[]` when nothing parsed.

**`emit`** — parse one document, then encode that node back to YAML with a
default encoder. Result `{"out":<string>}`.

**`emit_indent`** — same, with `SetIndent(indent)` called first. `indent` is
required: a missing `indent` is a `ProtocolError` with the message
`emit_indent requires an indent`, and a negative one is a `ProtocolError` with
`indent must not be negative`. The distinction matters because `SetIndent(0)` is
a real call with an observable effect, so "absent" cannot be folded into "0".

**`emit_stream`** — decode every document, then encode all of them through one
encoder. Result `{"out":<string>,"error":<string|null>}`, where `out` holds
whatever was written before the failure. A parse error is reported in preference
to an emit error.

**`roundtrip`** — parse, emit, parse that output, emit again. Three shapes:

```json
{"first":<string>,"second":<string>,"stable":<bool>}
{"first":<string>,"reparse_error":<string>}
{"first":<string>,"reemit_error":<string>}
```

This feeds the emitter's own output back to the parser. Canonically indented,
canonically quoted text is a different input distribution from the YAML the
documents are written in, and it is where an emitter that writes text its own
parser reads differently shows up.
`stable` is byte equality of `first` and `second`.

### The node shape

A node is a JSON object whose keys appear in exactly this order:

```
kind, style, tag, value, anchor, alias, head, line, foot, l, c, content
```

`kind`, `style`, `l` and `c` are always present. `tag`, `value`, `anchor`,
`head`, `line`, `foot` are omitted when empty, and `content` is omitted when
there are no children. `alias` appears only on an alias node.

| key | source | notes |
| --- | --- | --- |
| `kind` | `Node.Kind` | `none`, `doc`, `seq`, `map`, `scalar`, `alias` |
| `style` | `Node.Style` | the integer bitmask, not a name |
| `tag` | `Node.Tag` | the resolved tag: `!!str`, `!!int`, … |
| `value` | `Node.Value` | the scalar's text |
| `anchor` | `Node.Anchor` | anchor name, without `&` |
| `alias` | `Node.Alias != nil` | always the bare literal `true` |
| `head` | `Node.HeadComment` | |
| `line` | `Node.LineComment` | |
| `foot` | `Node.FootComment` | |
| `l` | `Node.Line` | 1-based |
| `c` | `Node.Column` | 1-based |
| `content` | `Node.Content` | array of nodes |

`kind` is a name and `style` is a number, which looks inconsistent and is
deliberate: kinds are a closed set of five, styles combine.

```
TaggedStyle       1        LiteralStyle      8
DoubleQuotedStyle 2        FoldedStyle      16
SingleQuotedStyle 4        FlowStyle        32
```

They compose, and composite values do occur in the graded documents: `3` is a
tagged double-quoted scalar, `5` a tagged single-quoted one, `9` a tagged literal
block, `33` a tagged flow collection. Emit `style` as the sum, whatever it is —
the list above is what happens to be in there, not a set to switch on.

`alias` is the one place the encoding refuses to recurse. `&a [*a]` builds a node
whose alias target is its own container, so following `Alias` would not
terminate; the alias name is already in `value`, so nothing is lost. An alias
node's `content` is likewise not walked.

### Encoding

Responses are compared byte for byte, so the JSON rule is part of the protocol
and is written out here in full:

- no whitespace anywhere outside string literals
- keys in the order the shape above declares, never sorted
- bytes `0x20`–`0x7E` appear literally, except `"` and `\`, which are
  backslash-escaped
- `0x08 0x09 0x0A 0x0C 0x0D` are `\b \t \n \f \r`
- **every** other code point is `\uXXXX`, lowercase hex, surrogate pairs above
  U+FFFF

The last clause makes every response pure ASCII. That is the point: it removes
any question of how each side writes UTF-8, and it keeps a frozen expectation
file diffable.

Do not reach for a general-purpose JSON encoder unless you have checked it
against this rule. Go's `encoding/json` fails it three ways — it escapes `<`,
`>` and `&`, it escapes U+2028 and U+2029 unconditionally, and it replaces
invalid UTF-8 with U+FFFD — which is why the reference hand-writes its encoder
rather than calling the standard library. Whatever you write in Zig, the five
clauses above are the specification.

## Behaviour: byte-exact, against the Go implementation

Where this document and the Go code disagree, the Go code wins. It is what
produced the expectations.

The things most likely to bite, all confirmed against v3.0.1:

**Error strings are graded.** The shape is `yaml: ` then, when a line is known,
`line N: `, then the problem text:

```
yaml: line 2: mapping values are not allowed in this context
yaml: did not find expected node content
yaml: unknown anchor 'missing' referenced
```

The line number comes from the context mark when that mark's line is non-zero,
otherwise from the problem mark on the same condition, and **scanner** errors
report one line higher than they record. Read that condition literally: the test
is on the recorded line, not on whether a mark exists, and go-yaml records the
first line as 0. So every error on the first line prints no `line N: ` at all,
which is why only the first example above carries one — `"\/"` on line 1 gives
`yaml: found unknown escape character`, and the same input on line 2 gives
`yaml: line 2: found unknown escape character`.

The frozen cases reach 42 distinct problem texts, counted after stripping the
`yaml: ` and `line N: ` envelope; they are the libyaml messages, spelled as
go-yaml spells them. That count spans both places an error can be graded — the
`error` object of a failed request, and the error fields of a result that partly
succeeded — because both are compared byte for byte.

**`%YAML 1.2` is rejected.** The parser accepts major 1, minor 1, and nothing
else, so `%YAML 1.2` fails with `found incompatible YAML document` even though
the resolver implements 1.2 semantics. Do not fix this.

**`\/` is not an escape.** The scanner accepts 21 characters after a backslash
and `/` is not among them; `"\/"` fails with `found unknown escape character`.
JSON allows it, YAML 1.2 allows it, this library does not. Read them off
`scannerc.go` rather than from memory of another YAML library — one of the 21 is a
literal tab, which shares its branch with `t`.

**Indent has two clamps that compose.** No `SetIndent` at all means 4.
`SetIndent(0)` also means 4 — zero is the encoder's "unset" marker. Then the
emitter clamps anything below 2 or above 9 to 2. So `SetIndent(1)`,
`SetIndent(10)` and `SetIndent(99)` all indent by 2, while `SetIndent(0)` indents
by 4. Both clamps are reachable from `emit_indent` and both are graded.

**Lines are never folded.** The default emitter width is `-1`, which becomes
`1<<31 - 1`, and the `Encoder` exposes no setter. No graded output wraps —
not plain scalars, not quoted ones, not folded block scalars, not flow
collections. A 200-column value stays on one line. Do not implement folding
against a width of 80; it will disagree with every long-line case.

**Depth is capped at 10,000, twice.** Flow level and indent stack each stop at
10,000 with `exceeded max depth of 10000`. Depth 10,000 must parse and 10,001
must fail, for both flow and block nesting. Note what this means in Zig: a
recursive-descent parser on the default stack will overflow long before 10,000,
and a segfault is not the graded answer. The reference reaches that depth without
trouble because Go grows its stacks; you will need an explicit stack, a larger
thread stack, or both.

**An empty stream is an emit error, not empty output.** `emit_stream` on `""`,
`"\n"`, `" "` or a comment-only input parses zero documents, so the encoder is
closed without a single event and reports
`yaml: expected STREAM-START`, with `out` empty.

**`Node.Value` keeps the lexeme.** `1.0`, `1.00`, `1e3` and `0x10` resolve to
their tags but keep their original text, and emitting replays that text. No
graded path formats a number, so no graded output depends on float formatting.
Resolve tags; do not normalise values.

**Comments are structural.** go-yaml v3 attaches comments to nodes as head, line
and foot, and the attachment rules are not obvious — a comment between two
mapping entries can belong to either. It is graded, and reading
`decode_test.go` and `node_test.go` is the fastest way to learn it.

### Not graded

These are real parts of the library, and the port does not need them. They are
listed so nobody spends a week on the wrong thing:

| surface | why not |
| --- | --- |
| typed `Marshal`/`Unmarshal` — anything but `*yaml.Node` | reproducing it means reproducing Go's `reflect` lattice and struct-tag rules; that is a Go runtime port, not a YAML port |
| `Node.Decode` into typed values | same; the Node-to-Node paths are graded instead |
| `sorter.go` | only reachable from the typed encoder |
| `!!timestamp` as `time.Time` | the resolved **tag** is graded; the Go calendar value is not |
| `document contains excessive aliasing` | unreachable through the graded surface: `unmarshal()` returns early for `*yaml.Node`, so the alias budget is never charged |
| `isOldBool` / `isBase60Float` quoting | only reached from the typed encoder's `stringv()`; the node emit path takes its style from the parsed node |
| `IsZeroer`, `ObsoleteUnmarshaler`, the v2 shims | part of the typed surface |

`sorter.go` is in the tree and must still be deleted with the rest of the Go.
Not graded means not tested, not "may stay".

## How this is scored

Three stages, out of 100. Each one is a gate on the next.

| stage | worth | what happens if it goes badly |
| --- | --- | --- |
| 1 audit | pass or fail | a failure scores **0** and the run stops |
| 2 behavioural | **40 points, or none** | short of every scored case the stage pays **0** and the run stops |
| 3 verification | **60 points** | 10 points each for the models that find nothing |

**Stage 1 is a reading gate, not a checklist.** A reviewer is given your
repository and the original side by side, and cannot run either one. They answer
eight written questions, all of them required, about whether the migration
actually happened: is the Go gone, is there Zig where it was, does the Zig
implement a YAML parser or delegate the work somewhere, is the build honest about
what it compiles. A file scan runs first and its findings are handed to the
reviewer as evidence, but the scan does not decide anything.

There is no partial credit here and no arithmetic. If Go sources remain, if a Go
toolchain runs at build time, if `@cImport` appears, if the original
implementation is found embedded as data, or if a table of expected responses is
baked in, the score is 0 and stages 2 and 3 do not run.

The reason is arithmetic elsewhere. A submission that keeps go-yaml and shells
out to it passes every differential case by *being* the reference. Any scheme
where audit is one summand among several pays that submission most of the
marks, so it is not one.

**Stage 2 is the differential**, worth 40: your probe's response bytes against
the Go library's, over four sets of inputs.

- **8,170** frozen cases in 17 families, answered by the reference when the
  verifier image was built.
- 2,000 cases generated at grading time from a seed you have never seen, answered
  by the reference **live, in the verifier container**. Memorising the frozen
  answers earns nothing here.
- Twelve whole documents under all six operations, graded by digest. 72 cases.
  Seven are shaped like config people keep — a Compose file, a GitHub workflow,
  GitLab CI, three Kubernetes manifests in one stream, an OpenAPI spec, a
  3,600-line Ansible inventory, a playbook — and each shape was picked for the
  hazard it carries: `on` as a key, merge keys, an anchor reused by four jobs.
  The other five drop the pretence and go at one thing each: directives and tag
  handles, comment attachment, the resolver's spelling table, a 26-document
  stream, and Unicode. All twelve were written for this task; none is a file
  scraped from a repository, so none of them is in a corner of the web your
  weights may have seen.
- 13 protocol cases: the handshake, a missing indent, a negative indent, an
  unknown op, a line that is not JSON.

10,255 cases in total, across 22 modules that are reported separately, so a report
tells you which families are weak and which are finished. Weight is distributed by
family rather than by case count, so a family with 38 cases and one with 2,484
carry comparable weight in that report and the rare corners are as visible as the
common ones. All of that is reporting, not pricing: the stage pays its 40 points
only for a submission that answered every scored case, so a port that parses and
cannot emit is paid what a port that does neither is paid, and passing 2,484 easy
cases is not most of the marks — it is none of them until the other 21 families
are finished too.

**Stage 3 runs only on a stage 2 at full marks** — every scored case answered.
Below that the differential has already found a disagreement, and paying six
models an hour each to look for another one buys nothing the behavioural report
does not already name. Six models each get an hour, your
repository, the original, and both builds. Each one reads the code and then tries
to write a single test — same protocol, through the same probe — that the
original passes and yours fails. Every model that fails to find one is worth 10
points to you.

Two things follow from how that is adjudicated. A candidate test only counts
against you if the original passes it, yours fails it, it reproduces three times,
and it stays inside the graded surface — so the surfaces listed under *Not
graded* above cannot be used against you, and neither can timing, error wording
the task never promised, or the iteration order of a Go map. And the models are
reading your code, not just running it: a path that returns a plausible answer
for the cases in the suite and a wrong one just outside them is exactly what an
hour of reading finds.

## Practical notes

- `README.swerefactor.md` in the repository root is a short pointer back to this
  document. It is not graded and you may delete it.
- The stub `yaml-probe` prints `yaml-probe: not implemented` and exits 70. It
  also drains stdin, so a harness that writes requests to it gets EOF rather
  than a hang. Keep that property in anything you write.
- `zig build` writes its cache outside the repository in this image
  (`ZIG_GLOBAL_CACHE_DIR` and `ZIG_LOCAL_CACHE_DIR` are set). Leave that alone;
  a cache inside the tree ends up in the collected submission.
- Work incrementally. `node` on scalars, then collections, then anchors and
  aliases, then comments, then `emit`, then the stream operations. Each is a
  family or several, and the report names the ones you have finished. Only a run
  that finishes all of them is paid.
- The six upstream `_test.go` files are 123 KB of documented behaviour and the
  single best resource in the repository. Read them before writing the scanner.
