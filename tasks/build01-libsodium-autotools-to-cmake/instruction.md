# Migrate this repository's build system from GNU Autotools to CMake

You are working in `/workspace/repo`, a complete C cryptography library
(libsodium 1.0.20). Today it is built exclusively by GNU Autotools:
`autogen.sh` → `autoreconf` → `./configure` → `make` → `make install`.

Your job is to **replace that build system with CMake** and to **delete the
Autotools one**. This is a build-toolchain migration, not a feature change: no
C source file needs new functionality, and the library that comes out the far
end must be indistinguishable from today's, down to its shared-object version,
its exported symbol set, and the machine code in each object file.

The result is rebuilt from scratch in a **container that has no autoconf, no
automake, no libtool and no `aclocal` installed**. Anything that still needs those
tools will simply fail there.

---

## 1. What must exist when you are done (State B)

### 1.1 A CMake build

* A top-level `CMakeLists.txt`, `cmake_minimum_required(VERSION 3.20)` or newer.
* Configures and builds with **both** the `Ninja` and the `Unix Makefiles`
  generator, and with `CMAKE_BUILD_TYPE` set to `Release`, `Debug` or empty.
* **Out-of-source only.** After a build in `<repo>/../build-x`, the source tree
  must be byte-for-byte what it was before the build: no generated headers, no
  object files, no `CMakeCache.txt`, no `Makefile` or `build.ninja`, no caches,
  and no source file rewritten in place. The verifier checksums the tree before
  and after and walks it looking for build output. To check the same thing
  yourself, use `git status --porcelain --ignored` or `git clean -ndx` against the
  baseline commit in your container -- **not** plain `git status`. The repository
  ships a `.gitignore` covering the Autotools build's products, so that an in-tree
  State A build does not bury your own changes in noise; the cost is that plain
  `git status` is silent about most of what this section forbids. `Makefile`,
  `src/libsodium/include/sodium/version.h`, `configure`, `aclocal.m4` and
  `Makefile.in` are all on that ignore list, and every one of them is collected
  and graded if you leave it in the source tree. The verifier does not consult
  `.gitignore`.
* Honours the standard toolchain entry points: `CC`, `CMAKE_C_COMPILER`,
  `CMAKE_C_FLAGS`, `CMAKE_INSTALL_PREFIX`, `DESTDIR`, and the
  `GNUInstallDirs` variables (`CMAKE_INSTALL_LIBDIR`,
  `CMAKE_INSTALL_INCLUDEDIR`).
* `cmake --build` succeeds with `-j$(nproc)` and is warning-clean enough that
  it does not fail; treat compiler warnings as non-fatal.

### 1.2 Cache options, mapped 1:1 onto today's `configure` switches

| CMake option            | Default | Equivalent to |
| ----------------------- | ------- | ------------------------- |
| `SODIUM_MINIMAL`        | `OFF`   | `./configure --enable-minimal` |
| `SODIUM_BUILD_SHARED`   | `ON`    | shared library (`--enable-shared`) |
| `SODIUM_BUILD_STATIC`   | `ON`    | static library (`--enable-static`) |
| `SODIUM_BUILD_TESTS`    | `ON`    | build the `test/default` suite |

Each must be a real cache option (visible in `cmake -LAH`) with exactly that
default. Each must work when it is the one thing changed from the defaults —
that is what gets built and installed: the defaults under both generators, and
then `SODIUM_MINIMAL=ON`, static-only, shared-only, `SODIUM_BUILD_TESTS=OFF`
and a `Debug` build, each measured separately. Do not make an option's meaning
depend on another's; at least one of `SODIUM_BUILD_SHARED`/`SODIUM_BUILD_STATIC`
will always be `ON`, and no combination should be one you have to have
anticipated.

### 1.3 Library artifacts, byte-for-byte equivalent in identity

With a default configuration and `--prefix=$P`:

```
$P/lib/libsodium.so.26.2.0      real shared object, SONAME = libsodium.so.26
$P/lib/libsodium.so.26          symlink -> libsodium.so.26.2.0
$P/lib/libsodium.so             symlink -> libsodium.so.26.2.0
$P/lib/libsodium.a              static archive
$P/lib/pkgconfig/libsodium.pc
$P/include/sodium.h
$P/include/sodium/…             (the full public header set)
$P/lib/cmake/libsodium/…        (see 1.6)
```

The version triple and SONAME are **not** yours to choose: derive them from
what the Autotools build produces today. `libsodium.la` is a libtool artifact
and must **not** be installed. Nothing outside the paths above (plus 1.6) may
be installed.

### 1.4 Feature detection must be real detection

Today `configure` probes the compiler and the platform and passes the results
to every translation unit as `-D` macros on the command line — there is no
`config.h`. Your CMake build must deliver **the same macro set, to every
library translation unit**, and must arrive at it by *probing*.

What "by probing" means here is a property of the result, not a list of
functions to call: change the compiler or the sysroot under the build and the
macro set has to change with it. The verifier configures your build against
compilers that reject specific flags and against sysroots missing specific
headers, and requires the difference to show up — hide `<sys/random.h>` and the
macro that depends on it must disappear; give it a compiler that refuses
`-mavx512f` and that flag must stop reaching the compile line. How you get
there is yours: CMake's own `check_c_source_compiles` / `check_include_file` /
`check_symbol_exists` / `check_c_compiler_flag` are the obvious way and nothing
scores the choice, but a hardcoded macro list cannot react and will read as a
failed migration.

Read `configure.ac` to learn which probes exist and which macro each defines.

### 1.5 Per-file ISA flags must stay per-file

The library contains several alternative implementations of the same
primitives — SSE2, SSSE3, SSE4.1, AVX, AVX2, AVX-512F, AES-NI, PCLMULQDQ,
RDRAND — selected at *runtime* by CPU dispatch. Each of those source files is
compiled today with its own `-m…` flags, and the rest of the library is
compiled with none of them.

That partition is part of the contract. If you raise the ISA baseline for the
whole library, the result crashes with `SIGILL` on any CPU lacking the
extension, and the verifier detects it by disassembling every object file.
`src/libsodium/Makefile.am` documents which source belongs to which ISA group.

### 1.6 A CMake package config for consumers

Installed under `$P/lib/cmake/libsodium/`:

* `libsodium-config.cmake` (or `libsodiumConfig.cmake`)
* a version file whose `PACKAGE_VERSION` is `1.0.20`
* the exported targets

`find_package(libsodium REQUIRED)` from a *separate* project must then work,
and `target_link_libraries(app PRIVATE libsodium::sodium)` must be sufficient
to compile and link against the installed library — the imported target must
carry its include directory. When both library kinds are built,
`libsodium::sodium` is the shared one and `libsodium::sodium_static` is the
static one; when only one kind is built, `libsodium::sodium` is that one.

### 1.7 pkg-config parity

The installed `libsodium.pc` must keep the same `Name`, `Version`,
`Description`, `Libs` and `Cflags` semantics as today's, so that
`pkg-config --cflags --libs libsodium` yields working flags. It must be
relocatable: express paths through `${prefix}` / `${exec_prefix}` /
`${libdir}` / `${includedir}`, never as absolute paths baked into the body.

Generate it from CMake. The top-level `libsodium.pc.in` and
`libsodium-uninstalled.pc.in` are Autotools templates (`@prefix@`,
`@exec_prefix@` come from `configure`'s substitution machinery, and
"uninstalled .pc" is an Autotools-only concept): delete both. A template of
your own that CMake consumes with `configure_file` is fine as long as it does
not live at the repository root under either of those two names.

### 1.8 The test suite, driven by CTest

`test/default` holds 80 test programs. Each writes its output to
`<name>.res` and compares it against `<name>.exp`, which it locates through the
`TEST_SRCDIR` preprocessor macro; a mismatch or a failed assertion is a
non-zero exit.

* `ctest` must register **one test per program, named exactly after the program**
  (`ctest -N` shows `auth`, `box_easy2`, `sodium_utils3`, …).
* Default configuration: **80** tests, all passing.
* `-DSODIUM_MINIMAL=ON`: **71** tests, all passing. The 9 that disappear are
  the ones today's `test/default/Makefile.am` guards behind `if !MINIMAL`;
  they must not be registered *or* built in a minimal configuration.
* `-DSODIUM_BUILD_TESTS=OFF`: no test targets, no test binaries, `ctest -N`
  lists nothing.
* `ctest -j$(nproc)` must pass — tests must not collide over shared files.
* Test binaries must not be installed by `cmake --install`.

### 1.9 A source package

`cpack --config CPackSourceConfig.cmake` (or `cmake --build . --target package_source`)
must produce a source archive that contains the library sources, public
headers and the test suite, and that does **not** contain build products,
version-control data, or the deleted Autotools files.

### 1.10 Optimisation and hardening flags must survive the port

`configure.ac` probes roughly fifteen compile and link flags with
`AX_CHECK_COMPILE_FLAG`, `AX_CHECK_LINK_FLAG` and `AX_ADD_FORTIFY_SOURCE`, and
every one of them ends up on the command line of **every** translation unit and
on **every** link. They are not cosmetic:

| what State A guarantees | why it matters |
|---|---|
| an optimisation level (`-O3`, falling back to `-O2`/`-O1`/`-O`) | the reference implementations are unusable unoptimised |
| `-fvisibility=hidden` | decides the exported symbol set (§3) |
| position-independent code (`-fPIC` / `-fPIE`) | the shared library and the PIE test programs |
| `-fno-strict-aliasing` | the reference code type-puns deliberately |
| `-fno-strict-overflow` (or `-fwrapv`) | wrap-around arithmetic must stay defined |
| `-fstack-protector` | overridable, the way `--disable-ssp` was |
| `_FORTIFY_SOURCE` at the highest level the libc accepts | why `libsodium.so.26` imports `__explicit_bzero_chk`, not `explicit_bzero` |
| `-Wl,-z,relro`, `-Wl,-z,now`, `-Wl,-z,noexecstack` | full RELRO and a non-executable stack in the shipped objects |

Reproduce them **by probing**, not by writing the flag strings into
`CMAKE_C_FLAGS`: a flag the compiler rejects must not be used, which is the
whole reason the autoconf probes exist. CMake's own abstractions count —
`POSITION_INDEPENDENT_CODE`, `C_VISIBILITY_PRESET` and an optimising
`CMAKE_BUILD_TYPE` are perfectly good ways to deliver three of these — but the
flag still has to arrive on the compile line, for all 117 C translation units,
under both generators, in the minimal configuration, and when the user also
passes `CMAKE_C_FLAGS` of their own. The library's 119 units are those 117 plus
two `.S` sources, and the two are exempt from the flags — automake assembled
them through `CPPAS`, not `CC` — but they must still be assembled and linked in.
(117 is also fewer than the `.c` files in the tree: `salsa20_ref.c` and
`salsa20_xmm6int-sse2.c` are the `!HAVE_AMD64_ASM` alternatives and are not
compiled on this host.)

`-DCMAKE_BUILD_TYPE=Debug` is the one configuration that backs off, as
`--enable-debug` did: no fortification without optimisation there. Visibility is
not negotiable in any configuration, because it decides the ABI.

---

## 2. What must no longer exist

Delete, from the source tree:

```
autogen.sh          configure.ac        Makefile.am (all of them)
m4/                 build-aux/          libsodium.pc.in
libsodium-uninstalled.pc.in
```

and any Autotools output that a previous build left behind (`configure`,
`Makefile.in`, `aclocal.m4`, `libtool`, `config.status`, `.deps/`, `.libs/`,
`*.la`, `*.lo`, `autom4te.cache/`). Every name in that list is gitignored, so if
you ran the State A build to observe it — as §5 suggests — plain `git status` will
not show you what it left. `git status --porcelain --ignored` will.

`src/libsodium/include/sodium/version.h.in` is **not** an Autotools artifact —
it is an ordinary substitution template, and CMake's `configure_file` consumes
it directly. Keep it, and keep generating `version.h` from it.

No build file, script or CMake module in State B may name `autoconf`,
`autoreconf`, `automake`, `aclocal`, `libtool`, `libtoolize` or `./configure`,
and no CMake code may shell out to them. Wrapping the old build system inside
CMake is not a migration.

That includes the helper scripts that ship with the repository:
`test/check-version-consistency.sh` treats `configure.ac` as its source of
truth for the version numbers. Once `configure.ac` is gone the script is dead
code — retarget it at the CMake build system (it must still cross-check the
version against `src/libsodium/include/sodium/version.h.in`,
`builds/msvc/version.h` and the packaging metadata) or delete it. Comments and
prose in `README`, `ChangeLog`, `THANKS`, `AUTHORS`, `LICENSE` and `CITATION`
may of course still mention the old toolchain.

---

## 3. What must not change

* **No C, C++ or assembly source may be edited.** Every `.c`, `.h` and `.S`
  file under `src/` and `test/` must keep its exact current content — they are
  checksummed. The only exception is that generated `version.h` may (and must)
  appear in your build directory.
* Public API and ABI: the exported symbol set, symbol visibility, and the
  SONAME stay as they are.
* Runtime behaviour: every test's output must still match its `.exp` golden.
* `README.markdown`, `LICENSE`, `AUTHORS`, `ChangeLog`, `THANKS` stay.

---

## 4. How the result is judged

Three things happen to the repository you leave behind, in order. First it is
**read**: State A's build system and yours are compared to decide whether the
migration described above actually happened — whether Autotools left rather than
moved, whether CMake describes the build rather than driving something else,
whether the forty compiler questions are asked rather than remembered. Then it is
**built**: configured across both generators and the option matrix, installed, and
measured against State A — the install tree, the symbol table, the macro set on
every translation unit, the ISA flags in every object, the 80 test programs
compiled from the grader's own pristine copy of `test/default`. Then it is
**attacked**: independent attempts are made to find something the Autotools build
does correctly and yours does not.

Everything is rebuilt from your sources. A build tree you leave behind is ignored,
and committed dependency directories are discarded. There is no partial credit
anywhere in this ladder — not for a repository that did not migrate, and not for
one that migrated and left a check failing — and no list of strings to avoid.
Write the build system you would ship.

---

## 5. Ground rules

* Work directly in `/workspace/repo`. Do not produce a patch file.
* Commit or don't commit; only the working tree is collected.
* **No network access.** Every tool you need is installed.
* The Autotools build still works right now — use it to observe State A's
  behaviour (`./autogen.sh -s && ./configure && make && make check`) before
  you replace it. `DO_NOT_UPDATE_CONFIG_SCRIPTS=1` keeps `autogen.sh` offline.
  Doing this is expected and costs you nothing, but it writes `configure`,
  `Makefile.in`, `aclocal.m4` and `autom4te.cache/` into the tree and rewrites the
  `build-aux/` helper scripts, and §2 requires all of that gone by the end. It is
  gitignored, so plain `git status` will not remind you — see §1.1.
* Be systematic. The library has 119 translation units in 7 ISA groups (117 C
  plus the two `.S`, as in §1.10), ~40 feature probes, 67 public headers and 80
  test programs. This is a whole-repository migration, and a half-migrated build
  system is not a partial result — it is a build system that does not work.
