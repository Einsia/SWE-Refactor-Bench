# Migrate sqlparse from Python to Go

`/workspace/repo` holds **sqlparse 0.5.3**, a non-validating SQL parser and
formatter: a Python package (`sqlparse/`), a command-line tool (`sqlformat`), a
documented public API, a hatchling build, and the packaging metadata for a
release.

Your job is to make the whole thing Go, and to leave it indistinguishable from
the outside.

Concretely: after your change, the parser must be a Go module whose import
closure is the standard library and itself, installing a single statically
linked `sqlformat`. The Python implementation must be gone from the repository —
not disabled, not kept behind a flag, not moved to a subdirectory. And the
product must behave exactly as it does today: same tokens, same tree, same
formatted bytes, same exit statuses.

This is a rewrite of a working system, not a feature. There is nothing new to
add. The difficulty is entirely in reproducing what is already there.

## 1. What the repository is

```
pyproject.toml          hatchling build; declares the sqlformat console script
sqlparse/               21 modules, 4024 lines
  __init__.py           parse, format, split, parsestream -- the public API
  cli.py                sqlformat(1)
  lexer.py              the regex-driven tokenizer
  keywords.py           1002 lines: the pattern table and the keyword dialects
  sql.py                660 lines: the node classes and their read surface
  tokens.py             the token-type lattice
  engine/               grouping.py (486 lines), statement_splitter.py, filter_stack.py
  filters/              reindent, aligned_indent, others, output, tokens
  formatter.py          option validation and filter-stack assembly
  utils.py, exceptions.py
docs/                   sqlformat.1 and the Sphinx sources
tests/                  the reference's own suite, 461 passing cases
examples/               two small scripts
```

Reading order that tends to work: `sqlparse/__init__.py` for the surface, then
`lexer.py` with `keywords.py` beside it (the tokenizer is a list of regexes tried
in order, and the order is load-bearing), then `engine/statement_splitter.py`
(how a stream becomes statements), `engine/grouping.py` (how a flat token list
becomes a tree, in a fixed sequence of passes), `sql.py` (the node types and
every accessor a caller can reach), and finally `formatter.py` over the
`filters/` it assembles. `tokens.py` is small and everything depends on it.

`git log` has one commit. The history is not available; the answer is not in it.

## 2. Hard requirements

**Go only.**
- The Go toolchain already in the image. A `go` directive between 1.22 and 1.24.
- The standard library and this module. **No third-party dependencies** — `go.mod`
  may declare no `require` beyond the toolchain line, and there is no network
  (`GOPROXY=off`, `GOSUMDB=off`). No `vendor/`, no `go.work`.
- `CGO_ENABLED=0`. No `import "C"`, no `#cgo` directive, no build tag that turns
  cgo on.
- These imports may not appear: `os/exec`, `plugin`, `syscall`, `unsafe`,
  `runtime/cgo`, `net`, `net/http`, `net/url`. A SQL parser needs none of them,
  and each is a way to ask something else for the answer.
- No prebuilt archive, object file or shared library may ship in the submission
  and be linked in.

**No Python may survive.** No `.py`, `.pyi`, `.pyc`, `.pyo` or `.pyd` file
anywhere in the repository, with no exceptions. `sqlparse/` must be gone — not
renamed, not moved under a subdirectory, not kept as reference material. Nor may
the installed command invoke an interpreter, embed one, or read a Python file at
run time. During the graded build every interpreter name on `PATH` is a shim that
fails and records the attempt, so a build that needs Python does not complete.

**These may go**, and are expected to: `sqlparse/`, `tests/`, `examples/`,
`pyproject.toml`, `.flake8`, `Makefile`, `TODO`. They are the old
implementation, its suite and its packaging. Ported equivalents under Go names
are welcome; at least one Go test file must exist.

**These must stay**: `LICENSE` byte-identical — the license does not change
because the language did — and `AUTHORS` byte-identical. The copyright line in the
header of every module under `sqlparse/` must still appear somewhere in the tree:
BSD-3-Clause asks that the notice be retained in redistributions, and every file
that currently carries it is a file you are expected to delete. Any file satisfies
this and a `doc.go` header is the usual place — it is an attribution requirement,
not a placement one. `CHANGELOG` must still carry its 0.5.3 entry (appending to it
is expected). `README.rst`,
`CONTRIBUTING.md` and `SECURITY.md` must still be present; `README.rst` will
have changed, since the install instructions are different now, but it is part of
the release and must not simply be deleted. `docs/sqlformat.1` must survive — at
that path or installed as `share/man/man1/sqlformat.1` — and must still describe
the command's real options.

**The public interface is specified, not invented.**
`/opt/swerefactor/source-contract.json` is readable in your workspace and is the
authority: it gives the module path, and for each package the exact set of
exported identifiers, with a note on each mapping it back to the Python it comes
from. That set is closed — no more, no fewer — in the same way the installed
header of a C library is closed. Unexported identifiers and any number of
`internal/...` packages are yours to arrange as you like. Read that file before
you design anything; it also publishes the encoding-alias table and the CLI's
error surface, both of which are decisions this task makes rather than facts a
port could derive.

Every package in the contract must carry a package comment, and most of its
exported functions and types must be documented. The reference documents 95% of
its public surface; you are held below that, not above it.

## 3. The build must keep working

Go is the build driver. All of these have to work from a clean checkout with no
network:

```sh
go build ./...              # succeeds with no output
go vet ./...                # no findings
gofmt -l .                  # names no files
go test ./...
go install -trimpath ./cmd/sqlformat
```

Each is graded independently, and so is a build from a cold module cache: the
module graph must resolve with `GOPROXY=off` and `GOFLAGS=-mod=mod`. A second
`go install -trimpath` must produce a byte-identical binary.

The installed tree must contain the command and the man page:

```
bin/sqlformat
share/man/man1/sqlformat.1
```

`bin/sqlformat` must be a real ELF64 executable, statically linked — no
`PT_INTERP`, no `PT_DYNAMIC`, no `DT_NEEDED` — carrying Go build information that
reports this module's path with `CGO_ENABLED=0` and `-trimpath=true`. It must not
be a shell script or a wrapper, must contain no CPython symbols anywhere in its
bytes, and must spawn no process when it runs.

## 4. Behaviour: byte-exact, against the Python

The reference is **sqlparse 0.5.3 as built from the Python sources you were
given**, executed by the grader. Not the documentation, not the SQL standard, not
a later release. Grading compares your output to the reference's byte for byte,
over a large generated corpus. Where the Python has a quirk, an oddity, or
behaviour its own documentation would arguably not predict, **reproduce the
quirk**. Compatibility is defined against the reference implementation.

That instruction has more force here than it usually does. The tokenizer is a
table of regular expressions evaluated in order, and it leans on four features of
Python's `re` that Go's `regexp` does not have: lookbehind, lookahead,
backreferences, and Unicode character classes. Several of the reference's
behaviours are consequences of how those patterns interact rather than decisions
anyone made — which token wins at a boundary, how a sign attaches to a number,
which characters a case-insensitive range happens to cover. You cannot port the
patterns; you have to port what they *do*, including where what they do is
surprising. Expect this to be where the time goes.

What is compared:

- **`format()`**, every documented option and combination of them: keyword and
  identifier case, comment and whitespace stripping, string truncation, operator
  spacing, the three reindent modes with their widths, tabs, column alignment,
  wrap points, comma-first, compact, right margin, and output language.
- **`split()`** and its statement boundaries: semicolons inside strings,
  comments, identifiers and dollar-quoted bodies; statements with no trailing
  semicolon; empty statements; `strip_semicolon`.
- **`parse()`** and the grouped tree: which nodes exist, how they nest, the token
  type each leaf carries, and the text each node covers.
- **The lexer's raw output** before grouping — every token type it can emit, in
  order, for any input — and the token-type lattice itself: the identity and
  rendered name of every type, and the parent-child relation between them.
- **The node read surface** as documented: `get_real_name`, `get_alias`,
  `get_name`, `get_parent_name`, `get_typecast`, `get_ordering`, `is_wildcard`,
  `has_alias`, `normalized`, `value`, `ttype`, and the `None` each returns where
  it does not apply.
- **The keyword tables**: which words are keywords in which dialect, the
  expressions that classify names, and the effect of registering more.
- **The filter stack**: each documented filter alone, and the documented stacks
  whole, at the stage the library installs it.
- **Option validation**: which values are accepted, and the exact message for
  those that are not.
- **Encoding handling**: bytes with and without a declared encoding, a BOM, an
  encoding the library does not know, and bytes that do not decode under the
  encoding they were given. An unknown encoding and an undecodable input are
  distinct outcomes and are graded separately.
- **Round-trip losslessness**: a parsed statement rendered back must reproduce
  its input exactly, for any input. This is the library's own central invariant.
- **`sqlformat(1)`**: every flag it documents, `--help` and `--version`, exit
  statuses, stdin and named files, and its behaviour on a file that does not
  exist or cannot be decoded.
- **Verification input**: deeply nested parentheses and case expressions, very
  long statements, pathological comment and string patterns, unterminated
  constructs. These must not crash, hang, or exhaust memory where 0.5.3 does not.

What is **not** compared: wall-clock time, beyond the requirement that a case
finish inside a generous timeout — you may be slower than the Python, and a
constant factor costs nothing. Memory as a number, allocation counts, and the
identity or address of any object. Text on stderr on a successful run, and the
wording of the diagnostics the CLI prints on failure — the exit status and the
fact that it said something are graded, the prose is not; the same goes for
argparse's phrasing of a usage error and the operating system's wording inside
"Failed to read". And the exact recursion depth at which the reference gives up
on nested input, which is a property of CPython's stack rather than of sqlparse:
what is graded is that ordinary nesting parses identically and that absurd
nesting produces an error rather than a crash or a hang.

`tests/` is the reference's own suite and a good oracle while you work. Grading
uses its own copy of the Python, so editing or deleting anything under `tests/`
changes nothing about your score.

## 5. How the result is judged

Three things happen to the repository you leave behind, in order.

First it is **read**. The Python you were given and the tree you leave are put
side by side and examined to decide whether the rewrite described above actually
happened — whether Go took over the default path, rather than wrapping,
vendoring, shelling out, or leaving the old implementation somewhere it can still
be reached. This stage judges the source and nothing else: it does not run your
build.

Then its **behaviour** is measured. Your source is built and installed in a clean
container, and the result is compared against the Python over the surface in the
previous section. This is where the credit is, it is graded by capability, and it
is measured on the artifact — what the binary does and what the built module
answers, not how either is written.

Then it is **attacked**. Independent attempts are made to construct an input that
the Python handles correctly and yours does not. A finding only counts if it
reproduces, and only if it lands on something this instruction promised: an
attack on your package layout, your type names, your performance, or your
diagnostic wording is out of scope, as is anything that identifies which of the
two trees it is looking at instead of asking the artifact a question.

There is no partial credit. A submission with a correct lexer, splitter and tree
that misses a reindent mode is paid what one that gets none of them is paid: the
behavioural stage pays only for every scored check, so the reindent mode is not a
slice of the score, it is the score. Write the implementation you would ship, and
finish it.

## 6. Practical notes

- Work in `/workspace/repo` directly. Commit or don't; only the working tree is
  collected, and build output is dropped when it is. Everything your build needs
  has to be in tracked source, because the grader builds from that source in a
  container where none of your build output exists.
- The reference is right there and it runs. Import it, call it, diff against it,
  and keep doing so for as long as it remains in your tree. Getting agreement
  with the Python before deleting it is the short path; deleting it first and
  working from memory is the long one.
- Two failure modes are worth learning early, because the environment has already
  been proven to produce them: a third-party import fails with `module lookup
  disabled by GOPROXY=off`, and a `go` directive above the local toolchain fails
  with `requires go >= ...` and `GOTOOLCHAIN=local`. Neither is a bug in the
  image.
- Port the pattern table by hand and test it against the Python as you go. It is
  the single largest source of small disagreements, and the four missing regex
  features mean there is no mechanical translation of it.
