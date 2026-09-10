#!/usr/bin/env python3
"""Generate downstream consumer projects.

Three kinds, all end-to-end:
  * pkg-config consumer  -> proves libsodium.pc is usable and relocatable
  * find_package consumer -> proves the exported CMake package works from a
    completely separate project, for both the shared and static targets
  * archive consumer -> the static half of the pkg-config route, which
    `pkg-config --libs` cannot express on its own: with both libraries in one
    libdir, `-lsodium` is the shared one and there is no field that says
    otherwise, so the archive is named on the link line where `-lsodium` was
"""
import os
import subprocess

from swerefactor.contract import submission_env

CONSUMER_C = r"""
#include <stdio.h>
#include <string.h>
#include <sodium.h>

int main(void) {
    unsigned char h[crypto_generichash_BYTES];
    const char *msg = "RepoMorphBench";
    if (sodium_init() < 0) { puts("INIT-FAIL"); return 1; }
    if (crypto_generichash(h, sizeof h, (const unsigned char *) msg,
                           strlen(msg), NULL, 0) != 0) {
        puts("HASH-FAIL"); return 1;
    }
    printf("%s|%d|%d|%d|%02x%02x\n", sodium_version_string(),
           sodium_library_version_major(), sodium_library_version_minor(),
           (int) sizeof h, h[0], h[1]);
    return 0;
}
"""

CMAKE_CONSUMER = """cmake_minimum_required(VERSION 3.20)
project(srb_consumer C)
find_package({pkg} REQUIRED{cfg})
add_executable(consumer consumer.c)
target_link_libraries(consumer PRIVATE {target})
"""


def write_consumer_source(directory):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "consumer.c")
    with open(path, "w") as fh:
        fh.write(CONSUMER_C)
    return path


def pkgconfig_consumer(directory, prefix, libdir, static=False, cc="cc",
                       relocated_prefix=None):
    """Build a consumer using pkg-config. Returns (rc, output, binary_path)."""
    src = write_consumer_source(directory)
    binary = os.path.join(directory, "consumer_pc")
    pcdir = os.path.join(libdir, "pkgconfig")
    env = submission_env()
    env["PKG_CONFIG_PATH"] = pcdir
    env["PKG_CONFIG_LIBDIR"] = pcdir
    if relocated_prefix:
        env["PKG_CONFIG_SYSROOT_DIR"] = ""
    args = ["--cflags", "--libs"]
    if static:
        args.insert(0, "--static")
    try:
        q = subprocess.run(["pkg-config", "libsodium"] + args, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "pkg-config failed: %s" % exc, binary
    if q.returncode != 0:
        return q.returncode, q.stdout.decode("utf-8", "replace"), binary
    flags = q.stdout.decode().split()
    argv = [cc, src, "-o", binary] + flags
    if static:
        argv.append("-static")
    try:
        p = subprocess.run(argv, cwd=directory, env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "link failed: %s" % exc, binary
    return p.returncode, " ".join(flags) + "\n" + p.stdout.decode("utf-8", "replace"), binary


def archive_consumer(directory, libdir, archive=None, cc="cc"):
    """Link a consumer against `libsodium.a` by name.  Returns (rc, out, binary).

    The static counterpart of :func:`pkgconfig_consumer`.  `--static --libs` gives
    the private dependencies the archive needs, but the archive itself still
    arrives as `-lsodium`, which the linker resolves to the shared library
    whenever both are in the same directory.  So the archive's path is
    substituted in at exactly that position and the rest of the line is left as
    pkg-config reported it, which keeps the link order pkg-config intended.

    `-static` is deliberately not passed: whether a static libc is installed is a
    fact about the image, and the question here is whether the *library* can be
    consumed statically.
    """
    src = write_consumer_source(directory)
    binary = os.path.join(directory, "consumer_ar")
    if archive is None:
        archive = os.path.join(libdir, "libsodium.a")
    if not os.path.isfile(archive):
        return 127, "no static archive at %s" % archive, binary
    pcdir = os.path.join(libdir, "pkgconfig")
    env = submission_env()
    env["PKG_CONFIG_PATH"] = pcdir
    env["PKG_CONFIG_LIBDIR"] = pcdir
    flags = []
    for args in (["--cflags"], ["--static", "--libs"]):
        try:
            q = subprocess.run(["pkg-config", "libsodium"] + args, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, "pkg-config failed: %s" % exc, binary
        if q.returncode != 0:
            return q.returncode, q.stdout.decode("utf-8", "replace"), binary
        flags.extend(q.stdout.decode().split())
    link = [archive if f == "-lsodium" else f for f in flags]
    argv = [cc, src, "-o", binary] + link
    try:
        p = subprocess.run(argv, cwd=directory, env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "link failed: %s" % exc, binary
    return p.returncode, " ".join(argv) + "\n" + \
        p.stdout.decode("utf-8", "replace"), binary


def pkgconfig_query(libdir, *args):
    pcdir = os.path.join(libdir, "pkgconfig")
    env = submission_env()
    env["PKG_CONFIG_PATH"] = pcdir
    env["PKG_CONFIG_LIBDIR"] = pcdir
    try:
        p = subprocess.run(["pkg-config", "libsodium"] + list(args), env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=120)
        return p.returncode, p.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def cmake_consumer(directory, prefix, target="libsodium::sodium",
                   pkg="libsodium", generator="Ninja", config=None,
                   extra_cmake=()):
    """Configure+build a separate CMake project against the installed package.

    Returns (rc, output, binary_path).
    """
    os.makedirs(directory, exist_ok=True)
    write_consumer_source(directory)
    cfg = " CONFIG" if config else ""
    with open(os.path.join(directory, "CMakeLists.txt"), "w") as fh:
        fh.write(CMAKE_CONSUMER.format(pkg=pkg, target=target, cfg=cfg))
    bld = os.path.join(directory, "b")
    argv = ["cmake", "-S", directory, "-B", bld, "-G", generator,
            "-DCMAKE_PREFIX_PATH=" + prefix, "-DCMAKE_BUILD_TYPE=Release"]
    argv.extend(extra_cmake)
    try:
        p = subprocess.run(argv, cwd=directory, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=600)
        out = p.stdout.decode("utf-8", "replace")
        if p.returncode != 0:
            return p.returncode, out, os.path.join(bld, "consumer")
        b = subprocess.run(["cmake", "--build", bld], cwd=directory,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=600)
        return b.returncode, out + b.stdout.decode("utf-8", "replace"), \
            os.path.join(bld, "consumer")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "ERROR: %s" % exc, os.path.join(bld, "consumer")
