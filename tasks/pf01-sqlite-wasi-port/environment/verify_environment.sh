#!/bin/sh
# Assert the premise of the task at image build time.
#
# pf01's State A is not broken: SQLite 3.31.1 builds and passes its own tests on
# Linux, and this script proves that first.  The debt is narrower and easier to lose
# track of than an ordinary port's -- the tree builds *for the host* and does not build *for the
# target* -- so four claims are checked rather than two:
#
#   1. the native toolchain is sound, and State A builds with it.  If this failed, a
#      submission could fail for a reason upstream of anything it did.
#   2. the wasm32-wasi toolchain is sound: it compiles and the result runs under the
#      same wasmtime the verifier scores with.
#   3. State A does *not* compile for wasm32-wasi with the POSIX layer it ships.
#      That gap is the task.  If a future wasi-sdk grew the missing syscalls, this
#      would stop failing and the task would have quietly lost its point.
#   4. the port is reachable: the amalgamation compiles for wasm32-wasi under
#      SQLITE_OS_OTHER=1, upstream's documented hook for supplying a VFS.  A task
#      whose premise holds but whose solution does not exist is not a task.
#
# Builds out of tree, in a temp directory.  /workspace/repo is read and never written:
# the agent must receive the pristine tree, not one this script configured.
#
# Runs once, at image build time, and is deleted in the same layer.  It is not a
# self-check: it asks nothing about a submission -- there is none yet -- and the agent
# never sees it or its output.
set -eu

repo="${1:?usage: verify_environment.sh /path/to/repo}"
SDK="${WASI_SDK_PATH:-/opt/wasi-sdk}"
SYSROOT="$SDK/share/wasi-sysroot"
WASMTIME="${WASMTIME_BIN:-/opt/wasmtime/wasmtime}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cd "$work"

fail() { echo "FATAL: $*" >&2; exit 1; }

# --- 0. the tree is the one we think it is ------------------------------------
for required in configure Makefile.in main.mk Makefile.msc manifest VERSION \
                src/os_unix.c src/os_win.c tool/mksqlite3c.tcl; do
  [ -e "$repo/$required" ] || fail "State A is missing $required"
done
version="$(tr -d ' \n' < "$repo/VERSION")"
[ "$version" = "3.31.1" ] || fail "expected VERSION 3.31.1, got '$version'"

# The five files that carry a copy of the source list, and the two sources whose
# removal is the closure half of the task.  Asserted present *and* referenced: if
# upstream had already stopped naming them, the build-file edits would be vacuous.
for build_file in Makefile.in main.mk Makefile.msc \
                  tool/mksqlite3c.tcl tool/mksqlite3c-noext.tcl; do
  grep -q 'os_unix' "$repo/$build_file" \
    || fail "$build_file does not name os_unix; the source list has moved"
done

# --- 1. native toolchain, and State A builds with it --------------------------
"$repo/configure" --disable-tcl --disable-readline > configure.log 2>&1 \
  || { tail -20 configure.log >&2; fail "configure failed on State A"; }
make sqlite3.c shell.c > generate.log 2>&1 \
  || { tail -20 generate.log >&2; fail "could not generate the amalgamation"; }
[ -s sqlite3.c ] || fail "sqlite3.c was not generated"

gcc -O2 -o sqlite3-native shell.c sqlite3.c -lm -lpthread -ldl > native.log 2>&1 \
  || { tail -20 native.log >&2; fail "State A does not build natively"; }
native_version="$(./sqlite3-native --version | cut -d' ' -f1)"
[ "$native_version" = "3.31.1" ] || fail "native build reports $native_version"
echo "select 'native ok';" | ./sqlite3-native > /dev/null \
  || fail "the native build cannot run a query"
echo "native:  ok (State A builds and runs: $(./sqlite3-native --version))"

# --- 2. the wasm32-wasi toolchain is sound ------------------------------------
[ -x "$SDK/bin/clang" ] || fail "no wasi-sdk clang at $SDK/bin/clang"
[ -d "$SYSROOT" ]       || fail "no wasi sysroot at $SYSROOT"
[ -x "$WASMTIME" ]      || fail "no wasmtime at $WASMTIME"

cat > hello.c <<'C'
#include <stdio.h>
int main(void) { printf("wasi ok %zu\n", sizeof(void *)); return 0; }
C
"$SDK/bin/clang" -O2 --target=wasm32-wasi --sysroot="$SYSROOT" -o hello.wasm hello.c \
  || fail "the wasi toolchain cannot compile a hello world"
got="$("$WASMTIME" run hello.wasm)" || fail "wasmtime cannot run what the sdk built"
[ "$got" = "wasi ok 4" ] \
  || fail "expected 32-bit wasm pointers ('wasi ok 4'), got '$got'"
echo "wasi:    ok ($("$SDK/bin/clang" --version | head -1), 32-bit pointers)"

# --- 3. State A does not compile for wasm32-wasi ------------------------------
# The amalgamation generated above, compiled for the target with the platform layer
# exactly as upstream ships it.  This is the whole debt in one command.
if "$SDK/bin/clang" -O2 --target=wasm32-wasi --sysroot="$SYSROOT" -I. \
     -c sqlite3.c -o would-not-exist.o > wasi-fail.log 2>&1; then
  fail "State A compiled for wasm32-wasi; the task has lost its premise"
fi
errors="$(grep -c 'error:' wasi-fail.log || true)"
[ "${errors:-0}" -gt 0 ] || fail "the wasm compile failed without emitting an error"

# Name the specific absences rather than counting errors, because the count is a
# property of clang's recovery and the absences are properties of WASI.  These four
# are what os_unix.c reaches for and wasm32-wasi does not have: memory mapping and
# the ownership model behind it.
for absent in mmap munmap fchown geteuid; do
  grep -q "undeclared identifier '$absent'" wasi-fail.log \
    || fail "expected '$absent' to be unavailable on wasm32-wasi; wasi-sdk has changed"
done
grep -q 'WASI lacks a true mmap' wasi-fail.log \
  || fail "sys/mman.h no longer refuses on wasm32-wasi; wasi-sdk has changed"
echo "debt:    confirmed ($errors errors compiling State A for wasm32-wasi;" \
     "mmap/munmap/fchown/geteuid absent)"

# --- 4. the port is reachable -------------------------------------------------
# Upstream's own porting hook.  With it, the platform-independent core compiles for
# the target cleanly and what remains is a VFS to write -- which is the task, and is
# the reason this is a port and not a rewrite.  A clean compile here is also why the
# five build-file edits can be pure deletions: no os_*.c enters the amalgamation.
"$SDK/bin/clang" -O2 --target=wasm32-wasi --sysroot="$SYSROOT" -I. \
    -DSQLITE_OS_OTHER=1 -DSQLITE_THREADSAFE=0 -DSQLITE_OMIT_LOAD_EXTENSION \
    -c sqlite3.c -o os_other.o > wasi-hook.log 2>&1 \
  || { grep 'error:' wasi-hook.log | head -10 >&2
       fail "SQLITE_OS_OTHER=1 does not compile for wasm32-wasi; the port is not reachable"; }
[ -s os_other.o ] || fail "SQLITE_OS_OTHER=1 produced no object"
echo "port:    reachable (SQLITE_OS_OTHER=1 compiles clean for wasm32-wasi:" \
     "$(stat -c%s os_other.o) bytes)"

# --- 5. no build entry point for the target already exists --------------------
# The submission writes this file; finding one already here would mean the payload
# was built from something other than the pristine upstream tree.
for handover in build-wasi.sh sqlite3.wasm; do
  [ -e "$repo/$handover" ] \
    && fail "State A already contains $handover; producing it is part of the task"
done

echo "State A: verified -- builds for the host, does not build for the target."
