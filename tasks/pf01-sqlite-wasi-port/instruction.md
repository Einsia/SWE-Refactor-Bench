# Port SQLite 3.31.1 from POSIX to wasm32-wasi

`/workspace/repo` holds the complete source distribution of **SQLite 3.31.1**
(2020-01-27), as upstream ships it. It is a POSIX program. Its storage layer —
`src/os_unix.c`, 7,904 lines — is built on file descriptors, `fcntl` record locks,
`mmap`, pthread mutexes, `getpid`, `geteuid`, `fchown` and `dlopen`. Beside it sits
`src/os_win.c`, another 6,135 lines for Windows. The build system assumes an autoconf
host and assembles the amalgamation with tclsh generators. The CLI shell reaches the
outside world through `popen` and `system`.

Your job is to make this tree build and run as a **wasm32-wasi command module** under
`wasmtime`, with the native platform layer gone rather than disabled, and with the
behaviour of the 3.31.1 release intact.

This is a port, not a rewrite and not a wrapper. Nothing about the SQL language, the
on-disk file format, the extensions, or the CLI is supposed to change. What changes is
the platform underneath them.

WASI preview1 is a genuinely smaller platform than POSIX, and the interesting work is
where it is smaller in a way that matters. Its filesystem is capability-based: a module
reaches only what the runtime preopened for it, and path resolution happens against a
preopen rather than against a global root. It has no process spawn, no `dlopen`, no
`mmap`, and no record locking. It is a 32-bit target.

## 1. What the repository is

```
src/                    157 files, 127 of them .c: the parser, b-tree, pager, WAL,
                        VDBE, the query planner, the CLI (shell.c.in), and the two
                        platform layers -- os_unix.c and os_win.c -- behind os.c,
                        os.h, os_common.h and os_setup.h
ext/                    14 extension trees: fts3, fts5, rtree, session, misc/ (json1,
                        sqlar, zipfile, dbstat, ...), lsm1, icu, rbu, userauth
tool/                   the build's own generators: lemon, mkkeywordhash,
                        mksqlite3c.tcl, mksqlite3h.tcl, mksourceid
test/                   upstream's Tcl test suite -- not what grades you
autoconf/               the amalgamation-tarball build
Makefile.in  main.mk  Makefile.msc  Makefile.linux-gcc  configure  configure.ac
manifest  manifest.uuid  VERSION
```

Reading order that tends to work: `src/os.h` and `src/os_setup.h` for how a platform
is selected at all, then `src/sqlite.h.in`'s VFS section for the interface you are
implementing (`sqlite3_vfs`, `sqlite3_io_methods`), then `src/os_unix.c` as the
reference implementation of that interface — it is the specification, in the sense that
whatever it does is what the rest of the tree expects. Then `src/pager.c` and
`src/wal.c`, which are the callers whose assumptions decide how much of the interface
you actually have to honour.

`git log` has one commit. The history is not available; the answer is not in it.

## 2. What must be true when you are done

### It builds for wasm32-wasi, from this tree, offline

The build contract is exactly this:

```sh
cd /workspace/repo && ./build-wasi.sh      # must exit 0
# and must have produced:
./sqlite3.wasm                             # a wasm32-wasi command module
```

`build-wasi.sh` does not exist yet; creating it is part of the task. It must work
offline, and it must work **with every build product deleted first**. The grader copies
your tree, removes the generated files from the copy — `sqlite3.c`, `sqlite3.h`,
`shell.c`, `parse.c`, `parse.h`, `opcodes.c`, `opcodes.h`, `keywordhash.h`, `fts5.c`,
`Makefile`, `config.*`, the compiled output — and then runs your script. A checked-in
`sqlite3.c` will not be there. Generating the amalgamation is where most of the
build-system work is actually exercised.

The toolchain, identical in the image you are in and in the image that grades you:
**wasi-sdk 24.0** (clang 18.1.2) at `$WASI_SDK_PATH`, a wasm32-wasi **zlib** at
`$WASI_DEPS`, **wasmtime 24.0.0**, GCC 12, GNU Make, `tclsh` 8.6, Python 3.11,
binutils, `file`, `pkg-config`, `git`, `curl`. No network. You cannot install anything.
Read `$WASI_SDK_PATH` and `$WASI_DEPS` from the environment rather than hard-coding
them.

How you organise the build underneath that contract is your decision. Driving the
repository's own makefile is one correct answer; calling clang directly with an
explicit file list is another. A cross-compile through State A's `configure` is harder
than doing it by hand, and neither route is worth points on its own.

The module that gets graded is the one the grader built from your sources. A
`sqlite3.wasm` you commit is deleted before your script runs.

### The scored invocation, so you can reproduce it exactly

Every case runs your module like this:

```sh
wasmtime run --dir <sandbox>::/data \
             --dir <tmpdir>::/tmp \
             --dir <sandbox>::.    \
             sqlite3.wasm <args...>
```

Three preopens: `/data` for databases, `/tmp` for temp files, and the same sandbox
again as the guest working directory so that relative filenames resolve. The third
grants no reach the first does not — it is one directory under two names — and without
it a bare `ar.db` has nothing to resolve against, while native, which simply runs with
its cwd set, succeeds.

The environment is pinned to
`LANG=C LC_ALL=C TZ=UTC PATH=/usr/bin:/bin HOME=/tmp TMPDIR=/tmp SQLITE_TMPDIR=/tmp`.
`TMPDIR` and `SQLITE_TMPDIR` are honoured, and temp files are expected to land in the
`/tmp` preopen. Nothing pins their filename pattern.

`wasi-run` on your `PATH` is this exact invocation, so a port developed against it is
developed against the thing that scores it. `wasm-imports.py <module>` prints a
module's import section — the complete list of everything it can reach outside itself.
`sqlite3-native` is native 3.31.1 built from this same tree: it is State A's own
behaviour, which is what you are preserving, so diff against it directly.
`/opt/toolchain.txt` records the versions.

### The behaviour of 3.31.1 is preserved

Measured by running your module and native 3.31.1 side by side, in the same container,
and comparing stdout, stderr, exit status, and the bytes of every file each one writes.
There is no expected-output file to match against and no list of strings to satisfy:
the reference is a binary built from the source you were given, and the comparison is
between two processes. What is compared:

- **The SQL language.** Expressions, operators, type affinity and coercion, the numeric
  tower, collations, joins, subqueries, CTEs plain and recursive, window functions,
  triggers, views, UPSERT, generated columns, `ON CONFLICT`, every built-in scalar and
  aggregate, `printf`, the date and time functions, and the error text for all of it.
- **The on-disk format, exactly**, at every page size and both text encodings. A
  database your module writes must be byte-identical to what native writes for the same
  operations, and native must be able to read it and pass `pragma audit_check`.
  This is cross-checked in both directions: databases native wrote — including a hot
  journal left behind by an interrupted native write, and a WAL file native
  checkpointed — must still be understood by your build.
- **WAL, as a working journal mode.** `journal_mode=wal` must report `wal`,
  checkpointing must work in all four modes, `wal_autocheckpoint` must take effect, and
  switching back to `delete` must remove the `-wal` and `-shm` files. WASI has no
  shared memory, but WAL's requirement is that the wal-index be shared between
  *connections*, and inside one instance that is a solvable problem. Do not omit WAL.
- **Every other journal mode** 3.31.1 supports — `delete`, `truncate`, `persist`,
  `memory`, `off` — the name each reports, the files each leaves on disk, and rollback
  in each. Plus transactions and savepoints: nested savepoints, release, rollback to a
  savepoint, deferred and immediate transactions, statement-level rollback.
- **The temp-file path through the VFS.** A query that spills, a large sorter, temp
  tables, temp triggers, `pragma temp_store` at its permitted values, and a temp file
  that has to be deleted on close.
- **Locking, as it is observable through the product.** Two connections in one process,
  a read while a write transaction is open, `pragma locking_mode`, the busy handler and
  its timeout.
- **The extensions this release builds.** FTS3/4, FTS5 with its ranking functions and
  every tokenizer the release ships, RTREE, JSON1, `dbstat`, `stmt`, `sqlar`, `zipfile`,
  `.archive`, `dbpage`, `deserialize` with the `memdb` VFS it brings, `sqlite_offset()`,
  and the comment column `explain` fills in. Each sits behind its own flag and a tree
  that quietly stops defining one still builds cleanly, so `pragma compile_options` is
  compared too.

  The exact flag set is not a guess: `/opt/scripts/build-native-oracle.sh` in this image
  is the recipe the reference is built with, and the verifier builds the reference with
  the same file. Match it. Five of its flags — `SQLITE_HAVE_ZLIB`,
  `SQLITE_ENABLE_DESERIALIZE`, `SQLITE_ENABLE_DBPAGE_VTAB`,
  `SQLITE_ENABLE_OFFSET_SQL_FUNC`, `SQLITE_ENABLE_EXPLAIN_COMMENTS` — have no entry in
  3.31.1's `src/ctime.c`, so `pragma compile_options` does not list them on either side
  and cannot be used as the checklist. Dropping one of those costs whole families of
  cases while the option list still matches. Going the other way costs as much: the
  option list is compared row for row, so a flag the reference does not set is a failure
  even when it enables something that works. `ext/session/` ships in the tree and is not
  built; `SQLITE_ENABLE_SESSION` and `SQLITE_ENABLE_PREUPDATE_HOOK` are both reported by
  `ctime.c`, so adding either breaks the option list and its count.
- **The CLI.** Every dot command, every flag, every output mode, `.dump`, `.import`,
  `.output`, `.read`, `.backup`, `.restore`, `.clone`, `.recover`, `.selftest`, the
  exact text of the error messages, and the exit status in each case. `shell.c.in` is
  the file a platform port has to touch most after the VFS — `popen`, `system` and
  `isatty` all live there — so it is the file most easily broken on the way past.
- **The mode bits and mtimes the product stores and restores**, as `writefile`,
  `readfile`, `lsmode`, `.archive`, `sqlar` and `zipfile` expose them.
- **Behaviour on verification input that native survives.** Deeply nested SQL, enormous
  blobs, very long strings, pathological `LIKE` and `GLOB` patterns, a corrupt database
  file, a truncated journal. A crash, a hang, or a wrong answer where native gives an
  answer is a defect.
- **`SQLITE_VERSION` and `SQLITE_SOURCE_ID`** as public API. See §4.

Where upstream 3.31.1 has a bug that is observable, **reproduce it**. Preserving
existing behaviour means the behaviour that exists, not the behaviour the documentation
describes. The corpus contains at least one case that requires an upstream bug to be
reproduced rather than fixed.

### The old platform layer leaves the tree

This is the requirement most likely to be underestimated, so it is stated concretely.

`src/os_unix.c` and `src/os_win.c` must be **gone** — not `#ifdef`'d out, not left in
place unbuilt, not renamed, not split up and pasted somewhere else. And removing them
is not a `rm`, because **five build files each carry their own hard-coded copy of the
source list**, in three different notations:

```
Makefile.in                  $(TOP)/src/os_unix.c   and   os_unix.lo
main.mk                      $(TOP)/src/os_unix.c   and   os_unix.o
Makefile.msc                 $(TOP)\src\os_unix.c   and   os_unix.obj
tool/mksqlite3c.tcl          os_unix.c              (bare, one per line)
tool/mksqlite3c-noext.tcl    os_unix.c
```

All of them. Including `Makefile.msc`, which nothing in either container builds — a
stale entry there is invisible until someone builds on Windows, and it is still a
reference to a file that no longer exists.

`src/os_win.h` **stays**. It is a header the rest of the tree references (`src/os.h`
includes it under `_WIN32`), not a platform implementation.

Upstream provides the hook: `SQLITE_OS_OTHER=1` compiles with no OS layer at all and
leaves `sqlite3_os_init()` to you. It is documented, and it compiles clean for
wasm32-wasi in this tree today.

### Your VFS is the default, and it is named `wasi`

Registering a new VFS *beside* the old default is not a migration. The VFS your port
registers must be named **`wasi`** and must be **first** in `.vfslist`, which is where
SQLite reports its default. The full expected list is `wasi`, `apndvfs`, `memdb`, in that
order, and the last two are not yours to register: `apndvfs` comes from the shell
(`ext/misc/appendvfs.c`, linked into `shell.c.in`) and `memdb` from the library
(`src/memdb.c`, compiled only under `SQLITE_ENABLE_DESERIALIZE` — so a build that drops
that flag is missing a VFS the list requires). Do not disturb either. Native, for
comparison, reports six: it also has `unix-excl`, `unix-dotfile` and `unix-none`, which
leave with `os_unix.c`.

That name is this benchmark's choice, not upstream's, and it is the one place the task
pins something the source leaves open. Everything else about the VFS is yours:
`sqlite3_os_init`/`sqlite3_os_end`, `szOsFile`, `mxPathname`, how the wal-index is
backed, and `SQLITE_IOCAP_POWERSAFE_OVERWRITE` are implementation decisions and are not
graded as such.

### Where WASI has no counterpart, remove the feature; do not fake it

Extension loading (`dlopen`), `mmap`, process spawn (`.shell`, `.system`, `.edit`,
`.excel`, and the `|command` forms of `.import` and `.output`) and threads have no WASI
equivalent. Removing them is correct. Leaving a code path that claims to support them
and fails at runtime is not: a method that returns `SQLITE_OK` without doing the work
passes whatever case does not depend on it and corrupts a database in the case that
does. Refuse honestly — `SQLITE_IOERR`, `SQLITE_NOTFOUND`, `SQLITE_CANTOPEN` — so the
engine can take another path.

The *headers* exist in wasi-sdk: `dlfcn.h`, `pthread.h` and `sys/file.h` all compile,
so "it linked" is not evidence the facility works.

Upstream provides macros for exactly these removals, and they are not interchangeable
in where you may apply them:

- `SQLITE_OMIT_LOAD_EXTENSION` and `SQLITE_THREADSAFE=0` belong to the **library**.
  Both are reported by `pragma compile_options`, both are expected there, and both are
  places where your build legitimately differs from the reference. A port that reported
  `THREADSAFE=1` without pthreads would be lying about its own build.
- `SQLITE_NOHAVE_SYSTEM` and `SQLITE_OMIT_POPEN` are **shell-only**. Every call site
  either macro guards is in `src/shell.c.in`; the library has none.

That second point is load-bearing, because `pragma compile_options` is itself compared —
its text, its line count, and its agreement with the reference outside the options the
platform forces. `src/ctime.c:598` reports `OMIT_POPEN`, so defining
`SQLITE_OMIT_POPEN` while compiling the amalgamation adds an option the library never
needed and fails three cases: the option list, its count, and the differential against
the reference. Define it where its call sites are. (`SQLITE_NOHAVE_SYSTEM` has no
`ctime.c` entry, so it is invisible either way — but the same reasoning applies to any
macro you are tempted to add globally. Check `src/ctime.c` first.)

`SQLITE_TEMP_STORE=2` or `=3` is **not** an acceptable way to deal with temp files.
Both push temp tables and the sorter into memory, which removes the VFS temp-file path
rather than porting it — and that path is part of what is measured. `pragma temp_store`
reports the compiled-in default, so this is visible.

## 3. Differences that are the platform's and are not defects

The two things being compared are an x86-64 ELF binary and a wasm32 module under an
interpreter. Some differences between them are forced by the target, and none of them
counts against a port. You do not need to reproduce native's answer for:

- **Pointer width and everything derived from it.** wasm32 is 32-bit; `pragma
  cache_spill`'s reported page count follows from the per-page cost.
- **`long double`.** 113 significand bits on wasm32 against 64 on x86-64. Two of
  SQLite's printf paths — `quote()`'s `%!.20e` fallback and `.mode quote`'s `%!.20g` —
  ask for more digits than a double has, so the two hosts disagree past the 17th
  significant digit. Renderings longer than 17 digits are re-derived from the value
  they parse to before comparison; the round-trip property this discards is asserted
  directly instead.
- **`argv[0]`.** The shell prints it at four sites, in two shapes: the `Usage:` banner
  and the `%s: Error:` prefix. Whoever launches the process chooses it, and the two
  launches are necessarily different. Both shapes are normalised. A bare `Error: ...`
  from the engine carries no prefix token and is compared as it is.
- **The four unix VFS names** — `unix`, `unix-none`, `unix-dotfile`, `unix-excl` — and
  any expectation that the default VFS is called `unix`. They belong to the layer you
  are deleting.
- **Thread support.** `THREADSAFE=0`, `pragma threads` pinned at 0, and a request to
  raise it ignored.
- **Wall-clock performance.** A wasm32 module under wasmtime is slower than native
  x86-64 by a large constant factor. That is expected; a hang that outlives a generous
  timeout is not.
- **Whatever wasmtime prints rather than the product** — a trap message, a runtime
  diagnostic, an exit status the engine synthesises for a trap.
- **Host filesystem artefacts** the product did not store: the exact permission bits
  wasmtime's filesystem layer carries through, a directory's reported size, and the
  order entries come back from a directory (`fsdir()`, `.archive -c`).

Of 52 behaviours probed on both targets, 48 turned out identical and are compared
byte-for-byte like everything else. Three are genuinely forced by the target and are
handled as above; the fourth was the toolchain's own name in `COMPILER=`, which is
dropped. The 78 cases whose expectation is written down rather than measured are the
ones where native's answer is *wrong for this platform* — `load_extension()` has to be
absent, not merely different — and each records what native answered beside it.

## 4. `SQLITE_SOURCE_ID`, which looks like a failure and is not

`sqlite3 --version` prints `SQLITE_SOURCE_ID`, and `tool/mksourceid` derives it from
the shipped `manifest`: it re-hashes every file `manifest` lists and marks the id when
any of them has moved. Deleting `src/os_unix.c` necessarily changes what that check
reports.

So does the payload you were given, for its own reason, so the reference binary already
reports the marked id. The two agree on a string neither was told, which is why this is
compared rather than written down — and it still catches the three things worth
catching: an id hard-coded into the port, a `manifest` regenerated against your own
tree, and a build that stopped deriving the id from `manifest` at all. Each changes a
value `sqlite3_sourceid()` publishes as public API.

`manifest` and `manifest.uuid` are not version-control metadata despite the names. They
are plain files upstream ships and the build reads: `tool/mksourceid` reads the first,
`ext/fts5/tool/mkfts5c.tcl` reads the second. Delete either and the build breaks.

## 5. How the result is judged

Three things happen to the repository you leave behind, in order.

First it is **read**. Both trees — the source you were given and the one you leave — are
opened by a reviewer that has no compiler, and the question is whether the migration
described above actually happened: whether the POSIX layer is gone rather than renamed
or pasted somewhere else, whether the VFS implements the interface or returns values
that make the caller stop asking, whether the engine was ported or edited until it
compiled, whether the module comes out of these sources, and whether anything in the
tree behaves differently when it thinks it is being graded. A submission that did not
migrate stops here.

Then its **behaviour** is measured. Both binaries are built in a clean container — yours
by your own `build-wasi.sh`, the reference from the source you were given — and compared
across the surface in §2. Credit is continuous and graded by capability: the modules are
separate, so a port with a working engine and a mangled shell scores like one, and the
layer being rewritten weighs more per case than the layer that should not have moved.
The corpus is fixed before your submission is seen and the denominator does not move —
a case that fails, a case that errors and a case the grader never reached cost the same,
and crashing partway through does not shrink what you are measured against.

Then it is **attacked**. Independent attempts are made to construct an input — a SQL
statement, a database file, an invocation, a sequence of dot commands — that the
reference handles correctly and yours does not. A candidate counts only if it passes on
the reference, fails on yours, and reproduces. The scope is §2 and §3: what §2 promised
is preserved is fair game, and what §3 calls the platform's is not. A probe that
distinguishes the two builds by reading their source, or by any other means of asking
"am I the wasm one?", establishes nothing and is rejected.

There is no partial credit anywhere in this ladder — not for a repository that
did not migrate, and not for one that migrated and left a check failing — and no
list of patterns to avoid. Write the port you would ship.

## 6. Practical notes

- Work directly in `/workspace/repo`. Do not produce a patch file.
- Commit or don't; only the working tree is collected. Build and generated output is
  dropped when it is — the amalgamation and the tclsh-generated sources, `configure`
  and `make` output, object files, archives, `.wasm`, `build*/`, `__pycache__`. So
  everything your build needs has to be in tracked source: the grader builds from that
  source in a container where none of your build output exists.
- There is no network. Everything you need is installed.
- Do not add third-party dependencies. zlib is already cross-built for wasm32-wasi at
  `$WASI_DEPS` and is load-bearing for `.archive`, `zipfile` and `sqlar` — `pragma
  compile_options` never mentions it, so it is easy to miss.
- Do not change the SQL language, the on-disk format, the CLI spellings, or the exit
  statuses.
- Do not special-case test-looking inputs, paths or arguments, and do not branch on
  anything that would tell you a grader is running: a directory name, a hostname, a uid,
  the number of preopens, whether stdout is a terminal. Reading `WASI_SDK_PATH` in a
  build script and `TMPDIR` in a VFS is ordinary configuration and is not that.
- Scratch work is fine anywhere in the tree, but everything you leave behind is part of
  the submission and is read as such — notes, experiments and a test harness of your own
  included. Nothing is exempt for being in a directory that looks temporary.
- Build and run things as you go. The native reference is on your `PATH` as
  `sqlite3-native`, the POSIX sources are right there to read for as long as they remain
  in your tree, and getting agreement with the reference *before* deleting them is the
  short path.

The tree is large and the platform surface runs through more of it than the two files
named above. Read before you edit: the interesting decisions are the ones where POSIX
and WASI semantics genuinely differ — path resolution against a preopen, locking,
temp files, timestamps, what a `wasi_snapshot_preview1` errno maps onto — rather than
the ones where a function name has to change.
