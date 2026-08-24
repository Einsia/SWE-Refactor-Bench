#!/bin/bash
# Build zlib for wasm32-wasi and install it where both images look for it.
#
# Run at image build time, in the agent image and the verifier image, from the same
# script and the same pinned tarball.  It has to be the same in both: the submission
# links against this libz.a and the verifier relinks against that one, so a version
# skew between them would fail a submission for the verifier's reason.
#
# Why zlib is here at all: 3.31.1's ctime.c has no entry for HAVE_ZLIB, so `pragma
# compile_options` lists the same ten rows whether or not the build has it and the
# option-list cases cannot see it -- but 73 cases across `.archive`, `sqlar` and
# `zipfile` can.  `.archive -c` deflates, and `ext.archive.compressed` asserts a
# compressed member reads back.  Without zlib the shell still builds, and those cases
# fail with an unhelpful error about an unsupported method.
#
# That is the port's half.  The reference has to carry the flag too, for a reason worth
# reading before touching either script: see the two-halves comment in
# build-native-oracle.sh, where leaving it off made those 73 cases compare two failures
# and pass while asserting nothing.
#
# zlib's configure is not a cross-compiling autoconf script -- it probes by running
# what it compiles, which cannot work when the output is wasm.  CHOST makes it skip
# the probes it cannot run; the remaining ones it gets wrong are overridden below.
set -euo pipefail

SRC_TARBALL="${1:?usage: build-wasi-zlib.sh <zlib-tarball> [prefix]}"
PREFIX="${2:-/opt/wasi-deps}"
WASI_SDK="${WASI_SDK_PATH:-/opt/wasi-sdk}"

# 1.2.11 specifically, and pinned by digest rather than by name: the task's expected
# values were recorded against a build linked with this one.
EXPECT_SHA=c3e5e9fdd5004dcb542feda5ee4f0ff0744628baf8ed2dd5d66f8ca1197cb1a1

echo "${EXPECT_SHA}  ${SRC_TARBALL}" | sha256sum -c -

[ -x "$WASI_SDK/bin/clang" ] || { echo "FATAL: no clang at $WASI_SDK/bin/clang" >&2; exit 1; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
tar -xzf "$SRC_TARBALL" -C "$WORK"
cd "$WORK"/zlib-*

SYSROOT="$WASI_SDK/share/wasi-sysroot"

# -D_WASI_EMULATED_* is not needed: the pieces of zlib SQLite uses are the deflate and
# inflate cores, which touch no file descriptors.  gzio does, and it is compiled, but
# nothing in the sqlar/zipfile path calls it.
export CC="$WASI_SDK/bin/clang"
export AR="$WASI_SDK/bin/llvm-ar"
export RANLIB="$WASI_SDK/bin/llvm-ranlib"
export CFLAGS="-O2 --target=wasm32-wasi --sysroot=$SYSROOT"
export CHOST=wasm32-wasi

./configure --static --prefix="$PREFIX" >configure.log 2>&1 || {
  echo "FATAL: zlib configure failed" >&2; tail -30 configure.log >&2; exit 1; }

# zlib's configure records what it found in zconf.h, not in CFLAGS: it rewrites two
# `#ifdef HAVE_*  /* may be set to #if 1 by ./configure */` lines to `#if 1`.  It gets
# both right here even though the probe compiles and runs a test program -- wasm32-wasi
# has unistd.h and stdarg.h, and the probes are compile-only for these two.
#
# Checked rather than assumed, because the failure is silent.  With them unset, zutil.h
# drops to a vsnprintf-free formatting path and gzio to one without unistd; the library
# still builds and still deflates, so nothing announces that the bytes came from a
# different code path than the one the expectations were recorded against.
for macro in Z_HAVE_UNISTD_H Z_HAVE_STDARG_H; do
  grep -q "^#  define ${macro}\$" zconf.h \
    || { echo "FATAL: configure left ${macro} unset in zconf.h.  Building anyway would" \
              "produce a zlib that works and compresses differently." >&2; exit 1; }
done

make libz.a >build.log 2>&1 || {
  echo "FATAL: zlib build failed" >&2; tail -40 build.log >&2; exit 1; }

# Prove it is wasm and not a host archive that happened to build.  An x86-64 libz.a
# links without complaint against nothing, and the failure would surface much later
# as an undefined symbol in the SQLite link.
"$WASI_SDK/bin/llvm-nm" --print-file-name libz.a >/dev/null
first_obj=$("$WASI_SDK/bin/llvm-ar" t libz.a | head -1)
"$WASI_SDK/bin/llvm-ar" x libz.a "$first_obj"
file_kind=$("$WASI_SDK/bin/llvm-objdump" -f "$first_obj" 2>&1 | grep -i 'file format' || true)
case "$file_kind" in
  *wasm*) : ;;
  *) echo "FATAL: libz.a is not wasm ($file_kind)" >&2; exit 1 ;;
esac

install -d "$PREFIX/lib" "$PREFIX/include"
install -m 644 libz.a "$PREFIX/lib/libz.a"
install -m 644 zlib.h zconf.h "$PREFIX/include/"

echo "zlib for wasm32-wasi -> $PREFIX"
echo "  libz.a  $(stat -c%s "$PREFIX/lib/libz.a") bytes"
echo "  sha256  $(sha256sum "$PREFIX/lib/libz.a" | cut -d' ' -f1)"
