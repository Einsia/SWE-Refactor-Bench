# Migrate cmark from C to Rust

`/workspace/repo` holds **cmark 0.31.1**, the reference implementation of
CommonMark: a C library (`libcmark`), a command-line tool (`cmark`), a public
header, a CMake build, and the packaging metadata for a release.

Your job is to make the whole thing Rust, and to leave it indistinguishable from
the outside.

Concretely: after your change, every byte of machine code in `libcmark.so`,
`libcmark.a` and `bin/cmark` must come from Rust source in this repository,
compiled by the pinned Rust toolchain. The C implementation must be gone from
the repository — not disabled, not kept behind a flag, not moved to a
subdirectory. And the product must behave exactly as it does today: same output,
same C ABI, same install tree, same package metadata.

This is a rewrite of a working system, not a feature. There is nothing new to
add. The difficulty is entirely in reproducing what is already there.

## 1. What the repository is

```
CMakeLists.txt          top-level build
src/                    19 library translation units, the private headers,
                        cmark.h (the public API), libcmark.pc.in,
                        cmarkConfig.cmake.in, the CLI's main.c
api_test/               the C API test program
man/                    man1/cmark.1 and man3/cmark.3
test/                   spec.txt, smart_punct.txt, regression.txt
data/                   CaseFolding.txt
fuzz/, bench/           fuzzing and benchmark harnesses
```

Reading order that tends to work: `src/cmark.h` for the surface, then
`blocks.c` (the block parser and the document lifecycle), `inlines.c` (the
inline parser, delimiter stack and reference resolution), `node.c` (the tree and
its invariants), then the five renderers (`html.c`, `xml.c`, `commonmark.c`,
`latex.c`, `man.c`) over the shared `render.c` machinery. `houdini_*.c` do
escaping and entity handling, `scanners.c` is a re2c-generated scanner, and
`utf8.c` and `cmark_ctype.c` are the byte-level primitives everything else sits
on.

`git log` has one commit. The history is not available; the answer is not in it.

## 2. Hard requirements

**Rust only.**
- Rust 1.90.0, edition 2021 or 2024.
- `std`, `core` and `alloc`. **No crates.io dependencies**, no git
  dependencies, no vendored third-party crates. There is no network.
- No C, C++, Objective-C or assembly translation unit may be compiled by the
  build that produces the shipped artifacts. Grading configures and builds your
  tree with `CC` and `CXX` pointed at a shim that records every compile it is
  asked to perform, so a build that still needs a C compiler is recorded as
  needing one and that is a finding against the rewrite. The environment you work
  in has a real C compiler on purpose: State A is your oracle, and §6 expects you
  to build it and diff against it. The rule is about what your build compiles,
  not about what the toolchain is able to do.
- No prebuilt object file, static archive or shared library may ship inside the
  submission and be linked in.
- No `dlopen`, no `LD_PRELOAD`, no helper process that does the parsing or
  rendering.

**These files must be gone** from the repository (an out-of-source build
directory is exempt, since the build produces it): every `.c`, `.cc`, `.cpp`,
`.h` except `src/cmark.h`, plus `.inc`, `.re`, `.o`, `.a`, `.so`, `.lib`,
`.dll`.

`src/cmark.h` stays. It is the installed public header and the ABI contract, and
its content must not change: no new symbols, no removed symbols, no changed
signatures, no version bump.

**These files must stay, unchanged in meaning**: `src/cmark.h`,
`src/libcmark.pc.in`, `src/cmarkConfig.cmake.in`, `man/man1/cmark.1`,
`man/man3/cmark.3`, `COPYING`.

**These may go**: `api_test/`, `fuzz/`, `Makefile.nmake`, `nmake.bat`,
`toolchain-mingw32.cmake`. Their purpose disappears with the C code. A Rust
equivalent is fine too.

## 3. The build must keep working, exactly as invoked today

CMake stays the build driver (3.25 or newer), out-of-source, and both of these
have to work from a clean checkout with no network:

```sh
# shared
cmake -S . -B build-shared -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=ON -DCMAKE_INSTALL_PREFIX=<prefix>
cmake --build build-shared --parallel
cmake --install build-shared

# static
cmake -S . -B build-static -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=OFF -DCMAKE_INSTALL_PREFIX=<prefix>
cmake --build build-static --parallel
cmake --install build-static
```

Both configurations are graded independently. Reconfiguring in an existing build
directory, rebuilding a second time with no changes, and building with
`-DCMARK_TESTS=OFF` all have to work. `cargo` runs offline
(`CARGO_NET_OFFLINE=true` is already set).

The install tree must contain exactly what it contains today:

```
bin/cmark
include/cmark.h  include/cmark_export.h  include/cmark_version.h
lib/libcmark.so.0.31.1   lib/libcmark.so -> libcmark.so.0.31.1   (shared)
lib/libcmark.a                                                   (static)
lib/pkgconfig/libcmark.pc
lib/cmake/cmark/cmark-config.cmake
lib/cmake/cmark/cmark-config-version.cmake
lib/cmake/cmark/cmark-targets.cmake
share/man/man1/cmark.1  share/man/man3/cmark.3
```

`libcmark.so.0.31.1` must carry that exact `SONAME`. It must export the
documented `cmark_*` entry points and **nothing else** — no Rust runtime
symbols, no `__rust_alloc`, no `rust_eh_personality`, no internal helper. Its
`DT_NEEDED` list must stay within the C runtime set it has today. Downstream
consumers must keep working unchanged: `pkg-config --cflags --libs libcmark`,
`find_package(cmark)` with the `cmark::cmark` target, and a hand-written
`-I<prefix>/include -L<prefix>/lib -lcmark`.

## 4. Behavior: byte-exact, against the C implementation

The reference is **cmark 0.31.1 as built from the C sources you were given**.
Grading compares your output to the reference's output byte for byte, over a
large generated corpus. Where the C implementation has a quirk, an oddity, or
behavior the CommonMark spec would arguably not require, **reproduce the
quirk**. Compatibility is defined against the reference implementation, not
against the specification prose.

What is compared:

- **`cmark(1)`**: every option and combination (`--to html|xml|man|latex|commonmark`,
  `--smart`, `--hardbreaks`, `--nobreaks`, `--unsafe`, `--safe`, `--sourcepos`,
  `--validate-utf8`, `--normalize`, `--width`, `--help`, `--version`), stdin and
  file input, several files concatenated into one document, exit statuses, and
  the exact text on stderr.
- **The C API**: all exported entry points. Document and streaming parsing
  (`cmark_parser_feed` in arbitrary chunk splits), the full node tree and every
  accessor, tree mutation (insert, unlink, replace, prepend, append) including
  the operations that are supposed to fail, iterators with
  `cmark_iter_reset`/`cmark_consolidate_text_nodes`, custom allocators through
  `cmark_mem`, and the NULL/empty/error paths.
- **The five renderers**, each over the whole corpus, including width-based
  wrapping in the commonmark renderer.
- **UTF-8 handling**: invalid sequences, overlong encodings, lone surrogates,
  NUL bytes, BOMs, truncated sequences.
- **Entities and case folding**: the full HTML5 entity table and the
  `CaseFolding.txt` data for reference-link label matching.
- **Pathological inputs**: the deeply nested and quadratic-blowup cases the C
  implementation has guards for, including its recursion and reference limits.
- **Source positions** (`--sourcepos`, `cmark_node_get_start_line` and friends),
  which have to match line and column for line and column.

`test/spec.txt`, `test/smart_punct.txt` and `test/regression.txt` are the
upstream conformance data. Grading uses its own copy of them, so editing them
changes nothing about your score.

## 5. How the result is judged

Three things happen to the repository you leave behind, in order. First it is
**read**: the C sources you were given and the tree you leave are compared to
decide whether the rewrite described above actually happened — whether the C
implementation left the default path and the Rust one took it over, rather than
wrapping, vendoring, shelling out or transliterating. Then its **behaviour** is
measured: both build configurations are configured, built and installed from your
source in a clean container, and the result is compared against the C
implementation byte for byte over the surface in the previous section. Then it is
**attacked**: independent attempts are made to construct a document, an API call
or an invocation that the C library handles correctly and yours does not.

There is no partial credit and no list of strings to avoid. Behaviour is graded by
capability and reported that way, so the report will tell you that a submission
got the parser and the five renderers right and missed a pathological-input
guard — but the points are paid only for one that missed nothing, so it is worth
what getting neither is worth. Write the implementation you would ship.

## 6. Practical notes

- Work in `/workspace/repo` directly. Commit or don't: only the working tree is
  collected, never the history, and build and install debris is dropped when it
  is — `build/`, `_build/`,
  `build-*/`, `cmake-build*/`, `target/`, `CMakeFiles/`, object files, archives,
  shared objects, install prefixes, and `.git/`. Everything your build needs has
  to be in tracked source, because the grader configures and builds from that
  source in a container where none of your build output exists. Note that
  `build.rs` is source and is kept; the patterns above drop directories whose
  names begin `build-`, not every name beginning `build`.
- Build and run things as you go. `cmake --build` and the reference behavior are
  both available to you — the C sources are right there, and you can build them,
  read them, and diff against them for as long as they remain in your tree.
  Getting agreement with the reference before deleting the C is the short path.
- The reference is the source you were given. When the spec and the C disagree,
  the C wins.
- Expect the long tail to be where the time goes: emphasis and delimiter runs,
  link reference definitions, HTML blocks, list tightness, tabs, and the
  renderers' escaping rules each have behavior that is easy to get almost right.
