#!/usr/bin/env python3
"""Migration-audit gates: was the work actually done?

The behavioural suite answers "does it still compress the same bytes".  It cannot
answer "was it rewritten", and on this task those come apart badly: a submission
that keeps zlib's C behind a JNI boundary passes every behavioral case by
construction, because the bytes are still being produced by the code that was
supposed to be replaced.  So the behavioural score is withheld unless the
mandatory gates here hold.

The gates are grouped by the claim each defends:

  G1  the C implementation has left the source closure
  G2  no C toolchain participated in producing the artifacts
  G3  the delivered classes carry javac provenance, no C provenance, and reach
      nothing outside java.base
  G4  one implementation, on the default path, unaware it is being graded
  G5  the release contract -- version, compile flags, API surface, module
      descriptor, license -- is intact

Two principles run through all of them.  First, prefer structural impossibility
to detection: the compiler shim makes compiling repository C *fail* rather than
merely logging it, so most of G2 is reading what the shim recorded.  Second,
prefer evidence over inference: every gate reports the specific paths, class
names, or log lines that decided it, because a gate that fails without saying why
is indistinguishable from a broken gate.

A gate that cannot run -- because the build failed, say -- fails closed.  Being
unable to demonstrate audit is not the same as demonstrating it, and the
alternative would let a submission that breaks the build inherit a pass.

Why the gates read the constant pool
------------------------------------
A jar offers no symbol table for translation-unit names, no DT_NEEDED for
delegation, no dynamic symbol table for dlopen and exec imports.  What it offers
instead is better rather than merely different: every type a class touches is named
in its constant pool whether the touching code runs or not, so no arrangement of
branches, lazy initialization or exception handlers removes the entry.  G3 is
therefore constant-pool scans.

What the constant pool cannot answer is what actually happened, and that question
is the sharp one here.  `java.util.zip` is a complete, bit-compatible deflate three
lines away and free -- the single most attractive cheat in the task -- so
`no-jdk-deflate` is checked twice: statically in the pool, and dynamically by
watching which classes the JVM loads during a compression round trip and
*differencing* that against a run that compresses nothing.  The differential is what
makes the observation usable: a JVM opens hundreds of files and loads a dozen zip
classes reading its own jars before any submitted code runs, so an absolute reading
would be all noise.  The same differential answers `no-file-access` and
`no-exec-helpers`, through an LD_PRELOAD interposer sitting below the JVM where
submitted Java cannot reach it.

G5 has no reference build to compare against.  A jar cannot be compiled against a
`.so`, so the pinned facts are read from the contract -- which Expectations.load()
has already cross-checked against its own several independent spellings -- and from
the delivered class files and the running JVM.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import classfile
import vlib
from build import (
    JAVA_MODES,
    JAVA_PROPERTIES,
    MODE_CLASSPATH,
    MODE_MODULE,
    BuildOutcome,
    java_command,
    java_compile,
    java_tool,
)
from structure import Expectations
from vlib import GateOutcome, Log

RUN_TIMEOUT = 180.0
CC_TIMEOUT = 300.0
JAVAC_TIMEOUT = 300.0

# The verifier's own compiler, by absolute path.  The shim shadows `cc` on PATH
# for the duration of the submission's build; the interposer below is not
# repository code and is compiled with the real thing, exactly as the reference
# probe is.
REAL_CC = "/usr/bin/cc"

# File magics, for the gates that must recognize a compiled artifact whose
# extension was removed.  A `.class` renamed to `.txt` is still a class file, and
# the source-closure gates are about what is in the tree rather than what it is
# called.
MAGIC_CLASS = b"\xca\xfe\xba\xbe"
MAGIC_ELF = b"\x7fELF"
MAGIC_ARCHIVE = b"!<arch>\n"
MAGIC_ZIP = b"PK\x03\x04"

# Compiled artifacts that must not be checked into a source submission.  The
# contract's extension list is the source of truth and is read at run time; this
# is the sniffing counterpart, used on files whose suffix says nothing.
BINARY_MAGICS = (MAGIC_CLASS, MAGIC_ELF, MAGIC_ARCHIVE, MAGIC_ZIP)

# Directories the build creates.  Excluded from source-closure scans because
# their contents are outputs rather than submitted source -- but still scanned
# for provenance, where being an output is exactly the point.
BUILD_DIR_MARKERS = (
    "CMakeCache.txt", "CACHEDIR.TAG", "build.ninja", ".ninja_deps",
)

# Names that mark a generated directory even when it holds no marker file.
#
# `target` and `build` are deliberately absent, unlike the Rust form.  verify.py
# already deletes those from the graded copy before any gate runs, so naming them
# here would be redundant -- and worse, `build` is a plausible name for a
# directory of build *scripts*, which is submitted source.  CMakeFiles is kept
# because it is CMake's own, always generated, and never submitted.
GENERATED_DIR_NAMES = {"CMakeFiles", "__pycache__", ".git", ".hg", ".svn"}

# Other compression implementations a submission might reach for instead of
# writing one.  The Java half of the list is the part that matters here: jzlib is
# a pure-Java zlib port that would pass every provenance gate in G3 and is still
# not a migration of this repository.
FOREIGN_IMPLEMENTATIONS = (
    "miniz", "zlib-ng", "zlibng", "libdeflate", "zopfli", "isa-l", "igzip",
    "jzlib", "jazzlib", "commons-compress", "xerial", "zstd", "brotli", "lz4",
    "snappy", "iq80", "aircompressor", "jcraft",
)

# Text in the submission that would mean it recognizes the evaluation rather
# than implementing the behavior.  Matched case-insensitively, so each pattern is
# specific enough not to fire on ordinary prose about testing.
#
# Every pattern here names something the author could not have read.  The agent is
# given instruction.md, `/workspace/repo` and a toolchain; none of them contains
# `ccshim`, `/logs/verifier` or a corpus blob path, so a file that carries one
# learned it from the harness and a mention anywhere -- comment included -- is the
# finding.  That is why this list is matched without exemption.
VERIFIER_AWARENESS_PATTERNS = (
    r"/logs/verifier",
    r"expectations\.(?:json|bin)",
    r"ccshim",
    r"CCSHIM_",
    r"\bblobs/\d{5}\.bin",
    r"corpus\.json",
    r"\bProbe\.java\b",
    r"\bSurface\.java\b",
    r"\bConsumer\.java\b",
    r"\bverify\.py\b",
    r"agent_run",
)

# The two the author *was* given, which is why they are not in the list above.
#
# instruction.md tells the author that `/opt/swerefactor/source-contract.json` is the
# normative statement of the API to implement, and to read it before writing code.
# So a javadoc line citing that path is a conscientious author quoting the
# specification, and failing it would fail a submission for doing what the
# instruction asked.  Both patterns nonetheless belong somewhere: a port that
# *reads* the contract at run time, or branches on whether it exists, is keyed on
# the grader's filesystem and would behave differently in a downstream install.
#
# So the rule for these two is dependence rather than mention: a match counts
# outside a comment, and a match in a delivered class file's constant pool always
# counts, because javac keeps string literals and discards comments -- a literal in
# the artifact is a value the code holds, not a note about one.
CONTRACT_PATH_PATTERNS = (
    r"swerefactor",
    r"source-contract\.json",
)

# Comment openers, per the languages TEXT_SUFFIXES admits: C and Java, CMake and
# shell and properties, and the continuation `*` of a javadoc block.
_COMMENT_OPENERS = ("//", "/*", "#", "--", "*")

# Java source that suggests answers are being recalled rather than computed.
#
# Narrower than the Rust list in one place and wider in another.  Narrower:
# `include_bytes!` has no Java counterpart, since a Java table is either an array
# literal or a resource, and the resource case is what getResourceAsStream covers.
# Wider: Base64 and ObjectInputStream are here because they are how a large table
# arrives in a Java source file without looking like one -- a 2 MiB string
# constant decoded at class-init time is a data file that a size scan of `.java`
# would find but a scan for `.bin` would not.
MEMOIZATION_PATTERNS = (
    r"\bMessageDigest\b",
    r"\bSHA-(?:1|256|512)\b",
    r"\bMD5\b",
    r"getResourceAsStream",
    r"\bgetResource\s*\(",
    r"ObjectInputStream",
    r"ObjectOutputStream",
    r"\bBase64\b",
    r"Files\.read(?:AllBytes|AllLines|String)",
)

# Text extensions worth scanning for source-level patterns.
TEXT_SUFFIXES = (
    ".java", ".txt", ".cmake", ".in", ".h", ".md", ".sh", ".py", ".cmakein",
    ".json", ".yml", ".yaml", ".cfg", ".mk", ".am", ".ac", ".properties",
    ".xml", ".gradle", ".kts", ".mf", ".map",
)

# Build files whose *name* makes them worth reading whatever their suffix.
BUILD_FILE_NAMES = (
    "cmakelists.txt", "makefile", "pom.xml", "build.gradle", "build.gradle.kts",
    "settings.gradle", "build.xml", "manifest.mf", "module-info.java",
)

# What the JVM legitimately touches during a compression round trip.  Paths under
# these prefixes are dropped from the interposer's ledger before the differential
# is taken, because they are the runtime rather than the library: /proc for the
# JVM's own accounting, the JDK tree for the modules image, /sys and /dev for
# CPU and entropy queries.
#
# The differential does most of the work -- a path that appears in both the
# measured run and the empty one is dropped whatever it is -- so this list is a
# second filter for the handful of paths a JVM touches nondeterministically
# rather than the primary mechanism.
JVM_ALLOWED_PREFIXES = (
    "/proc/", "/sys/", "/dev/random", "/dev/urandom", "/dev/null",
    "/usr/lib/jvm/", "/etc/ld.so", "/usr/lib/locale", "/usr/share/zoneinfo",
    "/etc/localtime", "/usr/lib/x86_64-linux-gnu/", "/lib/x86_64-linux-gnu/",
    "/etc/java", "/etc/timezone", "/etc/nsswitch.conf", "/etc/passwd",
    "/etc/resolv.conf", "/etc/hosts", "/usr/lib/locale/",
)

# The markers the observer opens around the measured window.  Nonexistent paths,
# opened and allowed to fail: the point is the record the interposer writes, not
# the file.  Everything the ledger holds between them happened while the
# submission's compression code was running, which is the only window any of this
# is a question about.
MARK_BEGIN = "/tmp/zguard-mark-begin"
MARK_END = "/tmp/zguard-mark-end"

# The forbidden package prefixes, in the slashed form a constant pool uses.
# Read from the contract at run time; this is the fallback for the helpers that
# run before it is loaded.
FORBIDDEN_PREFIXES_DEFAULT = (
    "java/util/zip/", "java/util/jar/", "sun/", "com/sun/", "jdk/internal/",
    "java/lang/foreign/",
)

# How to describe a forbidden reference in the failure message.  The static half
# of no-jdk-deflate deliberately reads the whole forbidden list, not just the
# compression entries, because it is the precise half of a pair whose dynamic half
# is narrowed to java.util.zip for measurement reasons.  That breadth is correct
# and load-bearing, but it means the gate can fail on a reference that has nothing
# to do with compression -- a ProcessBuilder, an Unsafe -- and a message that said
# "reaches into the JDK's own compression implementation" then named
# java/lang/ProcessBuilder as the evidence.  The verdict was right and the
# diagnosis was wrong, which is the kind of report that sends a submitter looking
# in the wrong file.
_REACH_GROUPS = (
    ("java/util/zip/", "the JDK's own compression implementation"),
    ("java/util/jar/", "the JDK's own archive implementation"),
    ("java/lang/foreign/", "the foreign-function linker"),
    ("sun/", "the platform's internal packages"),
    ("com/sun/", "the platform's internal packages"),
    ("jdk/internal/", "the platform's internal packages"),
    ("java/lang/Process", "another program"),
    ("java/lang/Runtime", "another program"),
    ("sun/misc/Unsafe", "the platform's internal packages"),
)


def _reached_into(offenders: list[str]) -> str:
    """Name the groups the offending references actually belong to.

    Offender lines carry the reference at the end, so the classification reads the
    line rather than being threaded through from the scan.  Anything unrecognized
    falls back to the generic phrase instead of being dropped, because a reference
    nobody anticipated is exactly the one worth mentioning.
    """
    hits: list[str] = []
    for line in offenders:
        matched = False
        for needle, phrase in _REACH_GROUPS:
            if needle in line or needle.replace("/", ".") in line:
                matched = True
                if phrase not in hits:
                    hits.append(phrase)
                break
        if not matched and "the platform's own implementation" not in hits:
            hits.append("the platform's own implementation")
    if not hits:
        return "the platform's own implementation"
    if len(hits) == 1:
        return hits[0]
    return ", ".join(hits[:-1]) + " and " + hits[-1]

# An interposer over the file-opening and process-spawning entry points,
# preloaded into the JVM so the compression paths can be watched from inside the
# process.  This is the only way to observe them without ptrace, which is not
# dependably available inside a grading container.
#
# It sits *below* the JVM, which is the property that makes it worth the C.  A
# Java-side observer -- a SecurityManager, an agent, a wrapped FileSystemProvider
# -- runs in the same JVM as the submission and can be uninstalled by it in one
# call.  This cannot be reached from bytecode at all.
#
# It is compiled here, from the verifier's own source, with the verifier's own
# compiler -- the shim is not on that PATH, and this is not repository code.
FILEGUARD_SRC = r"""
/* Record every path the process opens and every program it execs, so a gate can
 * assert the compression paths opened nothing and spawned nothing.  Written to
 * the file named by ZGUARD_LOG, appended, one "<what>\t<path>" line per call,
 * unbuffered -- a crash must not lose the record that explains it.
 *
 * The recorder reaches the kernel through syscall() rather than through open().
 * That is not a style choice: this object *defines* open, and a call to open()
 * from inside it binds to its own definition, so recording an open by opening
 * the log recurses until the stack is gone.  syscall() has no interposable
 * symbol to hijack, which makes the recorder structurally unable to observe
 * itself.
 *
 * A constructor writes a "load" line before main runs, so an empty log and an
 * unloaded interposer are distinguishable.  Without it, LD_PRELOAD silently
 * failing would look exactly like a submission that opened nothing, and the gate
 * would report a pass it had not observed.
 *
 * The exec family is interposed as well as the open family.  A JVM at steady
 * state execs nothing; ProcessBuilder on Linux reaches jspawnhelper through
 * posix_spawn, and Runtime.exec is ProcessBuilder underneath, so covering
 * posix_spawn and the execv* group covers both spellings.  fork and vfork are
 * deliberately not interposed: the JVM's own machinery uses them, and flagging
 * them would fail honest submissions for a runtime detail. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <spawn.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

static __thread int in_note;

static void note(const char *what, const char *path) {
  const char *log;
  long fd;
  char line[4200];
  size_t n = 0, i;

  if (in_note)
    return;
  in_note = 1;
  log = getenv("ZGUARD_LOG");
  if (log && path) {
    /* AT_FDCWD, O_WRONLY|O_APPEND|O_CREAT, 0644 -- straight to the kernel. */
    fd = syscall(SYS_openat, -100, log, O_WRONLY | O_APPEND | O_CREAT, 0644);
    if (fd >= 0) {
      for (i = 0; what[i] && n < sizeof(line) - 2; i++)
        line[n++] = what[i];
      if (n < sizeof(line) - 2)
        line[n++] = '\t';
      for (i = 0; path[i] && n < sizeof(line) - 2; i++)
        line[n++] = path[i];
      line[n++] = '\n';
      (void)syscall(SYS_write, (int)fd, line, n);
      (void)syscall(SYS_close, (int)fd);
    }
  }
  in_note = 0;
}

__attribute__((constructor)) static void zguard_load(void) {
  note("load", "fileguard");
}

int open(const char *path, int flags, ...) {
  static int (*real)(const char *, int, ...);
  if (!real)
    real = dlsym(RTLD_NEXT, "open");
  mode_t mode = 0;
  if (flags & (O_CREAT | O_TMPFILE)) {
    va_list ap; va_start(ap, flags); mode = va_arg(ap, mode_t); va_end(ap);
  }
  note("open", path);
  return real(path, flags, mode);
}

int open64(const char *path, int flags, ...) {
  static int (*real)(const char *, int, ...);
  if (!real)
    real = dlsym(RTLD_NEXT, "open64");
  mode_t mode = 0;
  if (flags & (O_CREAT | O_TMPFILE)) {
    va_list ap; va_start(ap, flags); mode = va_arg(ap, mode_t); va_end(ap);
  }
  note("open64", path);
  if (!real)
    return open(path, flags, mode);
  return real(path, flags, mode);
}

int openat(int dirfd, const char *path, int flags, ...) {
  static int (*real)(int, const char *, int, ...);
  if (!real)
    real = dlsym(RTLD_NEXT, "openat");
  mode_t mode = 0;
  if (flags & (O_CREAT | O_TMPFILE)) {
    va_list ap; va_start(ap, flags); mode = va_arg(ap, mode_t); va_end(ap);
  }
  note("openat", path);
  return real(dirfd, path, flags, mode);
}

FILE *fopen(const char *path, const char *mode) {
  static FILE *(*real)(const char *, const char *);
  if (!real)
    real = dlsym(RTLD_NEXT, "fopen");
  note("fopen", path);
  return real(path, mode);
}

FILE *fopen64(const char *path, const char *mode) {
  static FILE *(*real)(const char *, const char *);
  if (!real)
    real = dlsym(RTLD_NEXT, "fopen64");
  note("fopen64", path);
  if (!real)
    return fopen(path, mode);
  return real(path, mode);
}

/* The process-spawning half.  Recorded before forwarding, because an exec that
 * succeeds never returns and a record written afterwards would never exist. */
int execve(const char *path, char *const argv[], char *const envp[]) {
  static int (*real)(const char *, char *const[], char *const[]);
  if (!real)
    real = dlsym(RTLD_NEXT, "execve");
  note("exec", path);
  return real(path, argv, envp);
}

int execv(const char *path, char *const argv[]) {
  static int (*real)(const char *, char *const[]);
  if (!real)
    real = dlsym(RTLD_NEXT, "execv");
  note("exec", path);
  return real(path, argv);
}

int execvp(const char *file, char *const argv[]) {
  static int (*real)(const char *, char *const[]);
  if (!real)
    real = dlsym(RTLD_NEXT, "execvp");
  note("exec", file);
  return real(file, argv);
}

int posix_spawn(pid_t *pid, const char *path,
                const posix_spawn_file_actions_t *actions,
                const posix_spawnattr_t *attr,
                char *const argv[], char *const envp[]) {
  static int (*real)(pid_t *, const char *,
                     const posix_spawn_file_actions_t *,
                     const posix_spawnattr_t *, char *const[], char *const[]);
  if (!real)
    real = dlsym(RTLD_NEXT, "posix_spawn");
  note("spawn", path);
  return real(pid, path, actions, attr, argv, envp);
}

int posix_spawnp(pid_t *pid, const char *file,
                 const posix_spawn_file_actions_t *actions,
                 const posix_spawnattr_t *attr,
                 char *const argv[], char *const envp[]) {
  static int (*real)(pid_t *, const char *,
                     const posix_spawn_file_actions_t *,
                     const posix_spawnattr_t *, char *const[], char *const[]);
  if (!real)
    real = dlsym(RTLD_NEXT, "posix_spawnp");
  note("spawn", file);
  return real(pid, file, actions, attr, argv, envp);
}
"""

# The program the interposer watches, and the only Java the audit phase
# compiles.  Written here rather than shipped as an asset because it has to stay
# beside the gates that read its output: three of them depend on the exact shape
# of its records, and a file two directories away would drift from them.
#
# Three modes, and the difference between them is the measurement.
#
#   warm       compile-and-load smoke test; used by nothing, kept because it makes
#              the program runnable by hand while debugging a submission
#   noop       resolves one class literal from the jar and prints its name.  This
#              runs *no* submitted bytecode -- a class literal loads a class
#              without initializing it -- while still forcing the JVM to open the
#              jar and inflate an entry, which is what makes it a usable baseline
#              for the class-load differential.  Both facts are load bearing: the
#              JDK's own jar reading pulls in java.util.zip.Inflater, so a
#              baseline that did not read the jar would report that legitimate
#              load as the submission's cheat, and a baseline that ran submitted
#              code would give the cheat somewhere to hide.
#   roundtrip  warms every class and every lazy initializer with a throwaway
#              compression, opens the begin marker, does the *measured*
#              compression, opens the end marker, then prints its records.  The
#              markers bound the window the file and exec ledgers are read over,
#              so class loading -- which legitimately opens the jar -- falls
#              outside it and the window contains only what compression itself did.
#
# The API used is exactly the public surface the contract declares, reached the
# way a downstream would reach it.  Nothing here imports java.util.zip: an
# observer that borrowed the JDK's zlib would be comparing that library against
# itself.
OBSERVER_SRC = r"""
import org.zlib.Deflater;
import org.zlib.Inflater;
import org.zlib.Zlib;

/* Observer -- the subject of the dynamic audit gates.
 *
 * Deterministic in every respect that reaches stdout: the payload is generated
 * arithmetically rather than read, the records are fixed-format, and nothing is
 * printed that a clock, a hash code or an iteration order could influence.  The
 * environment-dispatch gate compares two runs of this program byte for byte, so
 * any nondeterminism here would read as a submission whose output depends on the
 * environment. */
public final class Observer {

    private static final int N = 65536;

    /* An open() the interposer will record, on a path that does not exist.  The
     * failure is the point: nothing is read, and the syscall is the mark.  The
     * exception classes are faulted in during the warm phase so that opening a
     * marker inside the measured window loads nothing. */
    private static void mark(String path) {
        try {
            java.io.FileInputStream in = new java.io.FileInputStream(path);
            in.close();
        } catch (Throwable ignored) {
            /* expected: the path does not exist */
        }
    }

    private static byte[] payload() {
        byte[] src = new byte[N];
        long seed = 12345L;
        for (int i = 0; i < N; i++) {
            if (i % 7 == 0) {
                seed = (seed * 1103515245L + 12345L) & 0xffffffffL;
                src[i] = (byte) (seed >>> 16);
            } else {
                src[i] = (byte) ('a' + (i % 23));
            }
        }
        return src;
    }

    private static long crc(byte[] buf, int len) {
        return Zlib.crc32(0L, buf, 0, len) & 0xffffffffL;
    }

    /* One streaming round trip at one level.  Returns the compressed length, or
     * a negative marker naming which call refused, so a broken API produces a
     * record rather than an exception and the caller can say which call failed. */
    private static long[] roundTrip(byte[] src, int level) {
        byte[] mid = new byte[N * 2];
        byte[] out = new byte[N];
        Deflater d = new Deflater();
        int rc = d.init(level);
        if (rc != Zlib.Z_OK) {
            return new long[] { -1, rc, 0, 0 };
        }
        d.nextIn = src;
        d.nextInIndex = 0;
        d.availIn = N;
        d.nextOut = mid;
        d.nextOutIndex = 0;
        d.availOut = mid.length;
        rc = d.deflate(Zlib.Z_FINISH);
        if (rc != Zlib.Z_STREAM_END) {
            return new long[] { -2, rc, 0, 0 };
        }
        long mlen = d.totalOut;
        d.end();

        Inflater f = new Inflater();
        rc = f.init();
        if (rc != Zlib.Z_OK) {
            return new long[] { -3, rc, 0, 0 };
        }
        f.nextIn = mid;
        f.nextInIndex = 0;
        f.availIn = (int) mlen;
        f.nextOut = out;
        f.nextOutIndex = 0;
        f.availOut = out.length;
        rc = f.inflate(Zlib.Z_FINISH);
        if (rc != Zlib.Z_STREAM_END) {
            return new long[] { -4, rc, 0, 0 };
        }
        long olen = f.totalOut;
        f.end();
        if (olen != N) {
            return new long[] { -5, olen, 0, 0 };
        }
        return new long[] { mlen, crc(mid, (int) mlen), crc(out, (int) olen), 0 };
    }

    private static void oneShot(byte[] src, StringBuilder sink) {
        byte[] mid = new byte[N * 2];
        byte[] out = new byte[N];
        int[] clen = new int[] { mid.length };
        int rc = Zlib.compress2(mid, clen, src, N, 6);
        if (rc != Zlib.Z_OK) {
            sink.append("oneshot rc ").append(rc).append('\n');
            return;
        }
        int[] ulen = new int[] { out.length };
        int rc2 = Zlib.uncompress(out, ulen, mid, clen[0]);
        sink.append("oneshot clen ").append(clen[0])
            .append(" ccrc ").append(String.format("%08x", crc(mid, clen[0])))
            .append(" ulen ").append(ulen[0])
            .append(" ucrc ").append(String.format("%08x", crc(out, ulen[0])))
            .append(" rc ").append(rc2).append('\n');
    }

    /* Every level, the one-shot helpers, and both checksums.  Called twice per
     * process: once to warm, once inside the marked window.  Returns the record
     * text so the warm call's output can be discarded and the measured call's
     * printed -- printing the warm output too would make the records depend on
     * how many times the payload had been compressed. */
    private static String measure(byte[] src) {
        StringBuilder sink = new StringBuilder();
        sink.append("version ").append(Zlib.version()).append('\n');
        sink.append("compileflags ")
            .append(String.format("%x", Zlib.compileFlags())).append('\n');
        for (int level = 0; level <= 9; level++) {
            long[] answer = roundTrip(src, level);
            if (answer[0] < 0) {
                sink.append("level ").append(level)
                    .append(" failed at ").append(answer[0])
                    .append(" rc ").append(answer[1]).append('\n');
                continue;
            }
            sink.append("level ").append(level)
                .append(" clen ").append(answer[0])
                .append(" ccrc ").append(String.format("%08x", answer[1]))
                .append(" ucrc ").append(String.format("%08x", answer[2]))
                .append('\n');
        }
        oneShot(src, sink);
        sink.append("adler ")
            .append(String.format("%08x", Zlib.adler32(1L, src, 0, N) & 0xffffffffL))
            .append('\n');
        sink.append("crc ").append(String.format("%08x", crc(src, N))).append('\n');
        return sink.toString();
    }

    public static void main(String[] args) {
        String mode = args.length > 0 ? args[0] : "roundtrip";

        if (mode.equals("noop")) {
            /* Class literals, not calls: these load four classes out of the jar --
             * opening the archive and inflating several entries, which is the
             * baseline the class-load differential needs -- and run none of their
             * code, because loading a class does not initialize it.
             *
             * Four rather than one so the baseline exercises at least as much of
             * the JDK's own jar-reading machinery as the measured run does.  If
             * the baseline read fewer entries, a zip class the JDK happened to
             * load on the second entry would appear only in the measured run and
             * be reported as the submission's. */
            Class<?>[] loaded = {
                Zlib.class, Deflater.class, Inflater.class, org.zlib.GzHeader.class,
            };
            StringBuilder names = new StringBuilder("noop");
            for (Class<?> c : loaded) {
                names.append(' ').append(c.getName());
            }
            System.out.println(names);
            System.out.flush();
            return;
        }

        byte[] src = payload();

        /* Warm: every class the measured window will touch, every lazy
         * initializer, and the marker's own exception path.  Whatever this opens
         * is outside the window on purpose. */
        String warm = measure(src);
        mark("/tmp/zguard-warm-probe");
        if (warm.isEmpty()) {
            System.out.println("warm produced nothing");
        }
        if (mode.equals("warm")) {
            System.out.print(warm);
            System.out.flush();
            return;
        }

        mark("/tmp/zguard-mark-begin");
        String measured = measure(src);
        mark("/tmp/zguard-mark-end");

        System.out.print(measured);
        System.out.flush();
    }
}
"""

@dataclass
class Observation:
    """One run of the observer under the interposer.

    `files` and `execs` cover only the marked window; `classes` covers the whole
    process, because a class load cannot be bracketed -- the JVM loads what it
    needs when it needs it, and the differential against the noop run is what
    separates the JVM's own loading from the submission's.
    """

    mode: str
    config: str
    ok: bool
    detail: str
    stdout: bytes = b""
    files: list[str] = field(default_factory=list)
    execs: list[str] = field(default_factory=list)
    classes: set[str] = field(default_factory=set)
    guard_loaded: bool = False
    # Set when the run did not happen because there was no jar to run against, as
    # opposed to happening and going wrong.  Carried on the result rather than
    # recomputed by the reader, because the observations are cached: the first gate
    # to ask reaches jar() and the ledger, and the four that ask afterwards get the
    # cached answer without touching it.  See IntegrityAuditor.evaluate.
    absent: str = ""


def _is_generated_dir(path: Path) -> bool:
    if path.name in GENERATED_DIR_NAMES:
        return True
    return any((path / marker).exists() for marker in BUILD_DIR_MARKERS)


def walk_source(root: Path) -> list[Path]:
    """Every submitted file, excluding directories the build produced.

    The distinction matters: a `.class` inside an out-of-source build directory is
    a normal build output, while the same file at the top of the tree is a
    prebuilt binary someone checked in.  Build directories are recognized by the
    markers their generators leave rather than by a guessed name, so a submission
    whose build directory has an unusual name is treated the same way -- and a
    submission that keeps its build *scripts* in a directory called `build` is
    not punished for the name.
    """
    out: list[Path] = []
    for current, dirs, files in os.walk(root):
        here = Path(current)
        dirs[:] = sorted(d for d in dirs if not _is_generated_dir(here / d))
        for name in sorted(files):
            out.append(here / name)
    return out


def rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def read_text(path: Path, limit: int = 8_000_000) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def in_comment(text: str, offset: int) -> bool:
    """Whether the match at `offset` sits in a comment, erring toward "no".

    Used only for CONTRACT_PATH_PATTERNS, where a mention is expected and a use is
    the finding.  The test is deliberately one-sided: a comment opener earlier on
    the line counts only if no quote character precedes it.  That way
    `String p = "http://opt/swerefactor/x";` is *not* excused -- the `//` is inside a
    string, the quote is what proves it, and the line stays a finding -- while
    `// see /opt/swerefactor/source-contract.json for the mapping` is.

    The asymmetry is the point.  A missed comment costs a report of something that
    could not affect behavior; a comment marker accepted at face value would let a
    real use hide behind one, so the quote check is what closes that off.  Nothing
    here parses the language, and it does not need to: for the two patterns this
    governs, the artifact's constant pool is checked with no exemption at all, and
    a Java statement that uses either path has to put the string there.
    """
    line_start = text.rfind("\n", 0, offset) + 1
    prefix = text[line_start:offset]
    for opener in _COMMENT_OPENERS:
        where = prefix.find(opener)
        if where < 0:
            continue
        before = prefix[:where]
        if '"' in before or "'" in before:
            continue
        if opener == "*" and before.strip():
            # A bare `*` is a javadoc continuation only at the start of the line;
            # anywhere else it is multiplication or a glob.
            continue
        return True
    return False


_FOREIGN_RE = re.compile(
    "|".join(
        rf"(?<![A-Za-z0-9]){re.escape(marker)}(?![A-Za-z0-9])"
        for marker in FOREIGN_IMPLEMENTATIONS
    ),
    re.IGNORECASE,
)


def foreign_markers(text: str) -> list[str]:
    """Which third-party implementation names appear as whole tokens.

    Token boundaries are not optional here.  A plain substring search finds
    `miniz` inside upstream's own `minizip` and `igzip` inside `minigzip`, both of
    which State A mentions in its README and its manual page -- so a substring
    scan would fail every submission that kept the documentation.  `-` and `_` are
    treated as boundaries rather than word characters, since the artifact names
    use them: `jzlib` has to match in `com.jcraft:jzlib:1.1.3`.
    """
    return sorted({m.group(0).lower() for m in _FOREIGN_RE.finditer(text)})


def result_text(*results) -> str:
    """Both streams of one or more commands, joined and decoded.

    `Result` keeps bytes, because a build's output is not guaranteed to be valid
    UTF-8 and decoding at capture time would lose the original.  The gates all
    want to grep it, so the decode happens here, once.
    """
    parts: list[str] = []
    for result in results:
        if result is None:
            continue
        parts.append(result.stdout.decode("utf-8", "replace"))
        parts.append(result.stderr.decode("utf-8", "replace"))
    return "\n".join(parts)


def count_java_logic_lines(paths: list[Path]) -> int:
    """Lines of Java that are neither blank nor comment-only.

    zlib is 13,192 hand-written C lines.  A port cannot be a few hundred lines of
    Java, so a floor is a cheap way to reject a stub that delegates elsewhere.
    Comments and blanks are excluded so documentation cannot inflate the count,
    and the block-comment handling covers Javadoc as well since `/**` starts with
    `/*`.
    """
    total = 0
    for path in paths:
        in_block = False
        for line in read_text(path).splitlines():
            stripped = line.strip()
            if in_block:
                if "*/" in stripped:
                    in_block = False
                continue
            if not stripped:
                continue
            if stripped.startswith("/*"):
                if "*/" not in stripped:
                    in_block = True
                continue
            if stripped.startswith("//"):
                continue
            total += 1
    return total


def _loaded_classes(log_path: Path) -> set[str]:
    """Class names out of a `-Xlog:class+load` file.

    Each line is `<binary name> source: <origin>`, and the name is the first
    token.  The origin is discarded deliberately: a submission's classes arrive
    from its jar and the JDK's from `jrt:/java.base`, but a cheat that copied
    java.util.zip source into its own package would arrive from the jar under a
    different name, and the *name* is what the forbidden list is written against.
    """
    names: set[str] = set()
    for line in read_text(log_path).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("["):
            continue
        head = stripped.split(None, 1)[0]
        if head and (head[0].isalpha() or head[0] in "_$"):
            names.add(head)
    return names


PRIMITIVE_DESCRIPTORS = {
    "void": "V", "boolean": "Z", "byte": "B", "char": "C", "short": "S",
    "int": "I", "long": "J", "float": "F", "double": "D",
}


def _type_descriptor(spelled: str) -> str:
    """`byte[]` -> `[B`, `java.lang.String` -> `Ljava/lang/String;`.

    The contract writes types the way Java source does, because it is read by
    humans deciding what to implement.  A class file writes them as descriptors.
    Both spellings are needed and this is the only place the conversion happens, so
    a gate comparing a contract type against a class file type cannot get it half
    right -- which would be a comparison that never matches and a check that never
    fires.

    Nested types are the case worth naming: the contract spells them
    `org.zlib.ZStream$Allocator`, with the `$` already, so no guess about which dot
    separates a package from an outer class is required here.
    """
    name = spelled.strip()
    depth = 0
    while name.endswith("[]"):
        depth += 1
        name = name[:-2].strip()
    if name in PRIMITIVE_DESCRIPTORS:
        core = PRIMITIVE_DESCRIPTORS[name]
    else:
        core = "L" + name.replace(".", "/") + ";"
    return "[" * depth + core


def _descriptor_of(params: list[str], returns: str) -> str:
    """A method descriptor from the contract's parameter and return spellings."""
    return "(" + "".join(_type_descriptor(p) for p in params) + ")" + _type_descriptor(
        returns
    )


class IntegrityAuditor:
    """Evaluates every declared audit gate against one submission."""

    def __init__(
        self,
        repo: Path,
        outcomes: dict[str, BuildOutcome],
        baseline: Path,
        expectations: Expectations,
        contract: dict,
        scratch: Path,
        probe_src: Path,
        log: Log,
    ) -> None:
        self.repo = repo
        self.outcomes = outcomes
        # An untouched extraction of the pinned tarball.  Every "unchanged"
        # comparison is made against this rather than against text restated in
        # the verifier, so there is exactly one definition of State A.
        self.baseline = baseline
        # The contract, already cross-checked against itself.  This replaces the
        # reference install trees of the C-to-C and C-to-Rust forms: there is no
        # reference jar to compare a delivered jar against, because the reference
        # publishes a shared object.  What the expectations give the G5 gates is a
        # statement of the release contract that has at least been checked for
        # internal agreement -- see Expectations.load.
        self.expectations = expectations
        self.contract = contract
        self.scratch = scratch
        # Kept for symmetry with the structure phase's auditor, which compiles the
        # probe from here.  This phase compiles its own observer instead, because
        # the probe answers API questions and the observer answers questions about
        # what the JVM did -- but a gate that wanted the probe would find it here.
        self.probe_src = probe_src
        self.log = log
        self._files: list[Path] | None = None
        self._java_files: list[Path] | None = None
        self._jar_cache: dict[str, classfile.JarFile | None] = {}
        self._fileguard: Path | None = None
        self._fileguard_built = False
        self._observer_dir: dict[str, tuple[Path | None, str]] = {}
        self._observations: dict[tuple[str, str], Observation] = {}
        self._base_packages: set[str] | None = None
        # Absences noticed while the current gate ran; see jar() and evaluate().
        self._absent_evidence: list[str] = []
        # "" for a graded submission; a sentence for the operator's reference
        # self-test.  vlib.c_self_test states the three conditions.
        self.self_test = vlib.c_self_test(
            [o.prefix for o in outcomes.values() if o.installed],
            expectations.jar_relpath,
        )
        if self.self_test:
            log.write(f"provenance: {self.self_test}")
        scratch.mkdir(parents=True, exist_ok=True)

    # -- shared reads -----------------------------------------------------

    @property
    def files(self) -> list[Path]:
        if self._files is None:
            self._files = walk_source(self.repo)
        return self._files

    @property
    def java_files(self) -> list[Path]:
        if self._java_files is None:
            self._java_files = [p for p in self.files if p.suffix == ".java"]
        return self._java_files

    def driver_packages(self) -> tuple[str, ...]:
        """The packages the contract's test drivers are declared in.

        Read from the driver contract rather than from a path convention: the
        contract names `org.zlib.test.Example` and `org.zlib.test.MiniGzip`, and a
        submission is free to keep its driver sources wherever its CMakeLists
        likes as long as the classes come out with those names.
        """
        packages: set[str] = set()
        for driver in self.expectations.contract.get("driver_contract", {}).get(
            "drivers", []
        ):
            name = str(driver.get("class", ""))
            if "." in name:
                packages.add(name.rsplit(".", 1)[0])
        return tuple(sorted(packages))

    @property
    def implementation_java_files(self) -> list[Path]:
        """The Java that becomes the library, with the test drivers left out.

        Most source-level gates want every `.java` in the tree, because a bundled
        answer table or a `System.loadLibrary` is a finding wherever it appears.
        One does not: the drivers are *programs*, and a program is allowed to know
        things about its own invocation that a library is not.  The distinction is
        drawn on the driver packages the contract declares, so it cannot be
        widened by moving a file.

        Nothing is lost by the exclusion.  The drivers' own behavior is graded far
        more tightly than a grep could manage -- byte for byte against what
        upstream's C drivers printed -- and every other gate, `no-verifier-
        awareness` included, still reads them.
        """
        drivers = self.driver_packages()
        if not drivers:
            return list(self.java_files)
        declared = tuple(f"package {pkg};" for pkg in drivers)
        kept: list[Path] = []
        for path in self.java_files:
            text = read_text(path)
            if any(mark in text for mark in declared):
                continue
            kept.append(path)
        return kept

    def baseline_c_basenames(self) -> set[str]:
        """The C translation units State A shipped, by basename.

        Read off the baseline tree, not the contract's list: the contract names
        the top-level ones plus `test/`, and reading the tree cannot fall out of
        date with it.
        """
        return {path.name for path in self.baseline.rglob("*.c")}

    def installed_configs(self) -> list[str]:
        return [c for c, o in sorted(self.outcomes.items()) if o.installed]

    def jar(self, config: str) -> Path | None:
        """The installed artifact, and a note on the ledger when there is none.

        Every gate that reads the delivery reaches it through here, so recording
        the absence at this one point records it for all of them -- twelve of these
        gates already say "no installed jar to inspect" in so many words, and this
        is that sentence made machine-readable.  evaluate() reads the ledger.
        """
        outcome = self.outcomes.get(config)
        if outcome is None or not outcome.installed:
            self._absent_evidence.append(
                f"[{config}] nothing was installed")
            return None
        if not outcome.jar.is_file():
            self._absent_evidence.append(
                f"[{config}] {self.expectations.jar_relpath} is not installed")
            return None
        return outcome.jar

    def jar_classes(self, config: str) -> list[classfile.ClassFile]:
        """Every non-module class in one configuration's installed jar.

        Cached: eleven gates read the same class files, and parsing a constant
        pool per gate would be the slowest thing in this phase.  A jar that will
        not open is cached as an absence, so the gates that depend on it report
        "unreadable" rather than each paying for the same failure.
        """
        if config not in self._jar_cache:
            path = self.jar(config)
            handle: classfile.JarFile | None = None
            if path is not None:
                try:
                    handle = classfile.load_jar(path)
                except (OSError, classfile.ClassFileError, Exception):  # noqa: BLE001
                    handle = None
            self._jar_cache[config] = handle
        else:
            # Eleven gates read the same class files and the cache is why that is
            # affordable.  On a cache hit jar() is not called, so an absence that
            # was recorded once for the first gate has to be recorded again here or
            # the other ten fail with nothing on the ledger explaining it.
            if self._jar_cache[config] is None:
                self.jar(config)
        handle = self._jar_cache[config]
        if handle is None:
            return []
        try:
            return handle.classes()
        except (classfile.ClassFileError, OSError):
            return []

    def jar_handle(self, config: str) -> classfile.JarFile | None:
        self.jar_classes(config)  # populate the cache
        return self._jar_cache.get(config)

    def all_jar_classes(self) -> list[tuple[str, classfile.ClassFile]]:
        """(config, class) for every class in every installed jar.

        Both configurations, because a gate is a yes/no rather than a weighted
        score: a submission whose module-path jar is clean and whose class-path
        jar is not has still shipped the second one.
        """
        out: list[tuple[str, classfile.ClassFile]] = []
        for config in self.installed_configs():
            for cls in self.jar_classes(config):
                out.append((config, cls))
        return out

    def shim_events(self, config: str) -> list[dict]:
        outcome = self.outcomes.get(config)
        if outcome is None or outcome.shim_log is None:
            return []
        if not outcome.shim_log.is_file():
            return []
        events: list[dict] = []
        for line in outcome.shim_log.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events

    def submission_packages(self, config: str) -> set[str]:
        """The slashed package prefixes the submission's own classes live in.

        Needed by the dependency gate: a reference from one of the submission's
        classes to another is not a dependency on anything, and the only way to
        know which names are its own is to read them off the jar.
        """
        out: set[str] = set()
        for cls in self.jar_classes(config):
            package = cls.package
            if package:
                out.add(package.replace(".", "/") + "/")
        return out

    def base_packages(self) -> set[str] | None:
        """The packages java.base exports, from the JVM that will grade the jar.

        Asked of the runtime rather than listed here.  A hardcoded list would be a
        second, silently drifting copy of the JDK's module descriptor, and the
        gate it feeds -- "nothing outside java.base" -- is only as good as its
        idea of what java.base contains.  Returns None when the question could
        not be asked, and the gate fails closed on that rather than guessing.
        """
        if self._base_packages is not None:
            return self._base_packages or None
        result = vlib.run(
            [java_tool("java"), "--describe-module", "java.base"],
            env=vlib.base_env(), timeout=60.0, log=self.log,
            label="describe-java.base",
        )
        if not result.ok:
            self._base_packages = set()
            return None
        found: set[str] = set()
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            stripped = line.strip()
            # `exports java.lang` and `contains sun.nio.fs` both name packages
            # that exist in the module; the gate cares about reachability, so only
            # the exported, unqualified ones count.
            if stripped.startswith("exports "):
                rest = stripped[len("exports "):].strip()
                if " to " in rest:
                    continue
                name = rest.split()[0] if rest else ""
                if name:
                    found.add(name.replace(".", "/") + "/")
        self._base_packages = found
        return found or None

    # -- the gate table ---------------------------------------------------

    def evaluate(self, cases: list[dict]) -> list[GateOutcome]:
        results: list[GateOutcome] = []
        for case in cases:
            check = case["check"]
            mandatory = bool((case.get("params") or {}).get("mandatory", True))
            handler = getattr(self, f"gate_{check.replace('-', '_')}", None)
            if handler is None:
                results.append(
                    GateOutcome(
                        gate_id=case["id"],
                        check=check,
                        mandatory=mandatory,
                        passed=False,
                        detail=f"no handler implemented for gate '{check}'",
                    )
                )
                continue
            self._absent_evidence = []
            missing: list[str] = []
            try:
                passed, detail, evidence = handler()
            except vlib.NotApplicable as exc:
                passed, detail, evidence = False, exc.reason, []
                missing.append(exc.reason)
            except Exception as exc:  # a broken gate must not pass a submission
                passed = False
                detail = f"gate raised {type(exc).__name__}: {exc}"
                evidence = []
            # Only for a gate that failed.  A gate that passed while noticing an
            # absence -- no-corpus-answers finds no bundled answer table in a tree
            # with no jar -- reached a real verdict, and replacing it with "not
            # answered" would throw that away.
            if not passed:
                missing.extend(self._absent_evidence)
            self._absent_evidence = []
            results.append(
                GateOutcome(
                    gate_id=case["id"],
                    check=check,
                    mandatory=mandatory,
                    passed=passed,
                    detail=detail,
                    evidence=evidence,
                    # Recorded on every run so the report can say "unanswered"
                    # instead of asserting something about behaviour nobody
                    # observed.  What it costs is decided in driver.py, and only in
                    # the reference self-test does it stop costing anything.
                    not_applicable="; ".join(dict.fromkeys(missing)),
                )
            )
        return results


    # -- dynamic observation ----------------------------------------------
    #
    # Four gates need to know what the JVM *did* rather than what the jar says:
    # no-file-access, no-exec-helpers, the runtime half of no-jdk-deflate, and
    # no-env-dispatch.  All four are answered from the same two runs per
    # configuration, so the machinery lives here once and the gates read it.

    def _build_fileguard(self) -> Path | None:
        """Compile the LD_PRELOAD interposer, once, with the real compiler.

        REAL_CC deliberately bypasses the shim's PATH entry: this is the
        verifier's own source, not repository code, and the shim's refusal applies
        to the submission's build rather than to the verifier's instruments.
        """
        if self._fileguard_built:
            return self._fileguard
        self._fileguard_built = True
        src = self.scratch / "fileguard.c"
        out = self.scratch / "fileguard.so"
        src.write_text(FILEGUARD_SRC, encoding="utf-8")
        result = vlib.run(
            [REAL_CC, "-shared", "-fPIC", "-O1", "-o", str(out), str(src), "-ldl"],
            cwd=self.scratch, env=vlib.base_env(), timeout=CC_TIMEOUT,
        )
        self.log.record(result, "fileguard-compile")
        if not result.ok:
            self.log.write(f"fileguard compile failed: {result.tail()}")
            return None
        self._fileguard = out if out.is_file() else None
        return self._fileguard

    def _observer_classes(self, config: str) -> tuple[Path | None, str]:
        """Compile Observer.java against one configuration's jar.

        Both linkage modes are tried, module path first, and the first that
        compiles is used.  Not a convenience: the two modes fail apart in practice
        -- a jar with a broken module descriptor compiles on the class path and
        not on the module path -- and a dynamic gate that could only run in the
        mode a submission happened to break would be a gate that reports
        "unobservable" for a reason the submission chose.  Which mode was used is
        returned so the ledger can say so.
        """
        if config in self._observer_dir:
            return self._observer_dir[config]
        jar = self.jar(config)
        if jar is None:
            self._observer_dir[config] = (None, "")
            return None, ""
        src = self.scratch / "Observer.java"
        src.write_text(OBSERVER_SRC, encoding="utf-8")
        for mode in JAVA_MODES:
            out = self.scratch / f"observer-{config}-{mode}"
            shutil.rmtree(out, ignore_errors=True)
            result = java_compile(
                [src], out, self.log,
                label=f"observer-javac-{config}-{mode}",
                mode=mode, jar=jar,
                module_name=self.expectations.module_name,
            )
            if result.ok and (out / "Observer.class").is_file():
                self._observer_dir[config] = (out, mode)
                return out, mode
            self.log.write(
                f"observer would not compile against {config} in {mode} mode: "
                f"{result.tail(4)}"
            )
        self._observer_dir[config] = (None, "")
        return None, ""

    def _observe(self, config: str, mode: str) -> Observation:
        """Run the observer once under the interposer and read the ledgers.

        `mode` is the observer's own argument -- "roundtrip" or "noop" -- not a
        linkage mode.  Cached, because the differential asks for both and four
        gates ask for the differential.

        The JVM is given the same properties the behavioural executor gives it,
        plus two flags that exist for the measurement:

          -Xlog:class+load  the class-load ledger, written straight to a file so
                            it does not mix into the stdout the env-dispatch gate
                            compares
          -XX:-UsePerfData  stops the JVM creating /tmp/hsperfdata_root/<pid>,
                            which is a write the interposer would record and which
                            has nothing to do with the submission
        """
        key = (config, mode)
        if key in self._observations:
            answer = self._observations[key]
        else:
            answer = self._observe_uncached(config, mode)
            self._observations[key] = answer
        # Re-noted on every read, not only on the miss.  Five gates share these two
        # observations per configuration; the first pays for the run and reaches the
        # ledger through jar(), and the rest would otherwise be handed a cached
        # absence with nothing recorded about why they had no answer.
        if answer.absent:
            self._absent_evidence.append(answer.absent)
        return answer

    def _observe_uncached(self, config: str, mode: str) -> Observation:
        guard = self._build_fileguard()
        if guard is None:
            return Observation(
                mode=mode, config=config, ok=False,
                detail="the file-access interposer could not be built",
            )
        class_dir, link_mode = self._observer_classes(config)
        if class_dir is None:
            # Two reasons, and the report has to tell them apart.  Either there is
            # no jar -- nothing was observed and nothing can be concluded -- or
            # there is one and javac refused it, which is a finding about the jar.
            # Reported identically until now, and the four gates downstream turned
            # the first into substantive claims: no-file-access said "the
            # compression paths reach the filesystem" about a round trip that never
            # ran, and no-env-dispatch said "the library's behavior depends on the
            # environment" about two runs that both failed to start.
            if self.jar(config) is None:
                return Observation(
                    mode=mode, config=config, ok=False,
                    absent=f"[{config}] there is no installed jar to observe",
                    detail="there is no installed jar to run the observer against",
                )
            return Observation(
                mode=mode, config=config, ok=False,
                detail="the observer would not compile against the installed jar "
                       "in either linkage mode",
            )
        jar = self.jar(config)
        assert jar is not None  # _observer_classes returned a directory
        ledger = self.scratch / f"ledger-{config}-{mode}.log"
        classlog = self.scratch / f"classload-{config}-{mode}.log"
        for path in (ledger, classlog):
            if path.exists():
                path.unlink()
        argv = java_command(
            "Observer",
            mode=link_mode, jar=jar, class_dir=class_dir,
            module_name=self.expectations.module_name,
            properties=JAVA_PROPERTIES + (
                f"-Xlog:class+load=info:file={classlog}:none",
                "-XX:-UsePerfData",
            ),
        ) + [mode]
        run = vlib.run(
            argv, cwd=self.scratch, timeout=RUN_TIMEOUT,
            env=vlib.base_env(LD_PRELOAD=str(guard), ZGUARD_LOG=str(ledger)),
            log=self.log, label=f"observe-{config}-{mode}",
        )
        if not run.ok:
            return Observation(
                mode=mode, config=config, ok=False,
                detail=f"the observer would not run: {run.tail(6)}",
                stdout=run.stdout,
            )
        files, execs, loaded, windowed = self._read_ledger(ledger, guard, classlog)
        if not loaded:
            # An interposer that did not load would report an empty ledger, which
            # is indistinguishable from a clean run.  Fail closed instead.
            return Observation(
                mode=mode, config=config, ok=False,
                detail="the interposer did not load; the run could not be observed",
                stdout=run.stdout,
            )
        # Only the roundtrip mode has a measured window; noop returns before it
        # would open a marker, on purpose, because it exists to be a class-load
        # baseline and not a file-access one.  Demanding a window from it would
        # make every differential unobservable -- and would report that as an
        # interposer that failed to load, which is a different fault entirely.
        if mode != "noop" and not windowed:
            return Observation(
                mode=mode, config=config, ok=False,
                detail="the interposer loaded but the observer never reached the "
                       "measured window; nothing was observed to grade",
                stdout=run.stdout,
            )
        return Observation(
            mode=mode, config=config, ok=True,
            detail=f"observed in {link_mode} mode",
            stdout=run.stdout,
            files=files, execs=execs,
            classes=_loaded_classes(classlog),
            guard_loaded=True,
        )

    def _read_ledger(
        self, ledger: Path, guard: Path, classlog: Path
    ) -> tuple[list[str], list[str], bool, bool]:
        """Split the interposer's ledger into the marked window.

        Only what happened between the two markers is returned.  Everything
        before the begin marker is class loading, JIT warm-up and the observer's
        own priming, all of which legitimately open files; everything after the
        end marker is the JVM shutting down.  The window in between is the
        submission's compression code and nothing else.

        The two booleans are separate answers and the caller needs both: the
        third says the interposer was in the process at all, the fourth says the
        observer reached the measured window.  Collapsing them would report a
        program that returned early as a preload that failed, which sends whoever
        reads the report looking at the wrong half of the machinery.
        """
        files: list[str] = []
        execs: list[str] = []
        loaded = False
        inside = False
        saw_begin = False
        instruments = {str(ledger), str(guard), str(classlog)}
        for line in read_text(ledger).splitlines():
            if "\t" not in line:
                continue
            what, _, path = line.partition("\t")
            if what == "load":
                loaded = True
                continue
            if path == MARK_BEGIN:
                inside = True
                saw_begin = True
                continue
            if path == MARK_END:
                inside = False
                continue
            if not inside:
                continue
            if what == "exec":
                execs.append(path)
                continue
            if path in instruments:
                continue
            if path.startswith(JVM_ALLOWED_PREFIXES):
                continue
            files.append(f"{what} {path}")
        return files, execs, loaded, saw_begin

    def _differential(self, config: str) -> tuple[Observation, Observation] | None:
        """The measured run and the baseline run, or None if either failed.

        Both must succeed for a differential to mean anything, which is why this
        returns a pair or nothing: a gate that compared a good measurement against
        a failed baseline would be subtracting zero and reporting the JVM's own
        behavior as the submission's.
        """
        measured = self._observe(config, "roundtrip")
        baseline = self._observe(config, "noop")
        if not measured.ok or not baseline.ok:
            return None
        return measured, baseline

    def _observe_env(self, config: str, extra: dict) -> bytes | None:
        """The observer's stdout under a given environment, without the guard.

        No interposer and no class-load log here: this run exists to be compared
        byte for byte against another run of the same program, and every flag that
        writes a file is one more thing that could differ between them for a
        reason that is not the submission.
        """
        key = (config, "env:" + json.dumps(extra, sort_keys=True))
        if key in self._observations:
            cached = self._observations[key]
            return cached.stdout if cached.ok else None
        class_dir, link_mode = self._observer_classes(config)
        jar = self.jar(config)
        if class_dir is None or jar is None:
            self._observations[key] = Observation(
                mode="env", config=config, ok=False,
                detail="no observer to run",
            )
            return None
        argv = java_command(
            "Observer",
            mode=link_mode, jar=jar, class_dir=class_dir,
            module_name=self.expectations.module_name,
        ) + ["roundtrip"]
        run = vlib.run(
            argv, cwd=self.scratch, timeout=RUN_TIMEOUT,
            env=vlib.base_env(**extra), log=self.log,
            label=f"observe-env-{config}",
        )
        answer = Observation(
            mode="env", config=config, ok=run.ok,
            detail="" if run.ok else run.tail(6), stdout=run.stdout,
        )
        self._observations[key] = answer
        return run.stdout if run.ok else None

    def _observer_records(self, config: str) -> dict[str, str]:
        """The measured run's records, as a key -> line map.

        The observer prints `version 1.3.1` and `compileflags a9` before anything
        else, which is what the two G5 runtime gates read.  Parsed here so both
        read the same text the same way.
        """
        observation = self._observe(config, "roundtrip")
        if not observation.ok:
            return {}
        records: dict[str, str] = {}
        for line in observation.stdout.decode("utf-8", "replace").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            head, _, rest = stripped.partition(" ")
            records.setdefault(head, rest)
        return records


    # == G1: the C implementation has left the source closure ==============

    def gate_no_c_sources(self) -> tuple[bool, str, list[str]]:
        """No C, C++, Objective-C or assembly translation unit survives.

        Extension-based, and deliberately so: the claim being defended is not
        "no C was compiled" -- G2 defends that -- but "the C implementation is not
        here any more".  A submission that keeps adler32.c unreferenced by the
        build has not finished the migration; the next maintainer reading the tree
        still finds two implementations and no statement of which one is real.

        The compiled extensions are dropped from the set here because
        `no-prebuilt-objects` grades them, with magic sniffing this gate does not
        need.  What is left is exactly the translation units.
        """
        exts = {e.lower() for e in self.contract["forbidden_paths"]["extensions"]}
        exts -= {
            ".o", ".obj", ".a", ".lo", ".la", ".so", ".dylib", ".dll", ".jnilib",
            ".class", ".jar", ".jmod",
        }
        offenders = [
            rel(self.repo, p) for p in self.files if p.suffix.lower() in exts
        ]
        if offenders:
            return (
                False,
                f"{len(offenders)} C/C++/assembly translation unit(s) remain",
                offenders[:40],
            )
        return True, f"no forbidden translation unit among {len(self.files)} files", []

    def gate_no_private_headers(self) -> tuple[bool, str, list[str]]:
        """zlib's internal headers are gone; zlib.h remains.

        Two failure directions, and both are graded.  Keeping deflate.h means the
        C implementation's private structure survived, which is what this gate is
        looking for.  But deleting zlib.h means the specification itself was
        thrown away, and that is worse -- so the gate also requires the
        allowlisted header to still be present.

        The allowlist is one file here, not two.  Unlike the C-to-C and C-to-Rust
        forms, zconf.h.cmakein is *not* allowlisted: it configures a C compiler's
        view of the library, and there is no C compiler.  A submission that keeps
        it is not failed for it -- it is removable, not forbidden -- but it is not
        required either, so it is not looked for.
        """
        forbidden = set(
            self.contract["forbidden_paths"]["internal_headers_that_must_be_removed"]
        )
        allow = set(self.contract["forbidden_paths"]["header_allowlist"])
        present = {p.name for p in self.files}
        kept = sorted(forbidden & present)
        if kept:
            return (
                False,
                f"{len(kept)} internal C header(s) still present: {kept}",
                [rel(self.repo, p) for p in self.files if p.name in set(kept)],
            )
        missing = sorted(allow - present)
        if missing:
            return (
                False,
                f"the behavioural specification was deleted: {missing} missing",
                [],
            )
        return True, f"no internal C header; {sorted(allow)} intact", []

    def gate_no_c_in_build(self) -> tuple[bool, str, list[str]]:
        """CMake declares no C or C++ source, and no C language at all.

        Read from the generated build system rather than from CMakeLists.txt.
        Parsing CMake text would be guessing at a Turing-complete language; the
        generated build files name every compile CMake intends to perform, which
        is the actual answer.

        The Java form can ask for more than the Rust form could.  A Rust build
        still reaches a linker, so `as` and `ld` appearing in the rules meant
        nothing; a javac/jar build reaches no compiler and no linker at all, so
        CMakeCache.txt naming a C compiler is itself the finding.  It is reported
        as evidence rather than as a failure on its own, because `project(zlib C)`
        with no C source is a leftover declaration rather than a C build -- but a
        C *source* in the rules is a failure either way.
        """
        c_basenames = self.baseline_c_basenames()
        c_stems = {name[:-2] for name in c_basenames}
        offenders: list[str] = []
        notes: list[str] = []
        scanned = 0
        for config in sorted(self.outcomes):
            outcome = self.outcomes[config]
            if not outcome.configured:
                continue
            for path in outcome.build_dir.rglob("*"):
                if not path.is_file():
                    continue
                if path.name not in (
                    "build.make", "link.txt", "flags.make", "DependInfo.cmake",
                    "build.ninja",
                ):
                    continue
                scanned += 1
                text = read_text(path)
                for match in re.finditer(
                    r"([A-Za-z0-9_./+-]+\.(?:c|cc|cpp|cxx|S|s|m|mm))\b", text
                ):
                    name = Path(match.group(1)).name
                    # CMake's own compiler-identification sources live in
                    # CMakeFiles and are not repository code.
                    if name.startswith("CMake") or "CompilerId" in match.group(1):
                        continue
                    if Path(match.group(1)).stem in c_stems or name in c_basenames:
                        offenders.append(
                            f"{config}: {rel(self.repo, path)} -> {match.group(1)}"
                        )
            cache = outcome.build_dir / "CMakeCache.txt"
            if cache.is_file():
                for line in read_text(cache).splitlines():
                    if re.match(r"CMAKE_(?:C|CXX)_COMPILER:", line.strip()):
                        notes.append(f"{config}: {line.strip()[:120]}")
        if not scanned:
            return False, "no generated build system to read; the configure step failed", []
        if offenders:
            return (
                False,
                f"the generated build system still compiles {len(offenders)} C source(s)",
                sorted(set(offenders))[:40],
            )
        detail = f"{scanned} generated build file(s) name no zlib C source"
        if notes:
            detail += f"; the project still declares a C compiler ({len(notes)} entry)"
        return True, detail, sorted(set(notes))[:6]

    def gate_java_present(self) -> tuple[bool, str, list[str]]:
        """A substantial Java implementation exists.

        Both halves are needed.  Files alone are not evidence -- a submission can
        add an empty Zlib.java -- and a line count alone would accept one
        generated file.  The floor is set against State A's 13,192 hand-written C
        lines: a faithful port lands in the same order of magnitude, and anything
        under a few thousand lines is delegating the work elsewhere, which in this
        task means java.util.zip.
        """
        if not self.java_files:
            return False, "no .java file anywhere in the repository", []
        floor = int(self.contract["jvm_code_policy"]["min_java_logic_lines"])
        lines = count_java_logic_lines(self.java_files)
        if lines < floor:
            return (
                False,
                f"only {lines} non-comment Java line(s) across "
                f"{len(self.java_files)} file(s); floor is {floor}",
                [rel(self.repo, p) for p in self.java_files[:20]],
            )
        return True, f"{lines} Java logic line(s) in {len(self.java_files)} file(s)", []

    def gate_java_is_primary(self) -> tuple[bool, str, list[str]]:
        """The delivered jar comes out of the graded build's javac.

        Not a provenance read -- G3 does that from the class files.  What is graded
        here is that the Java toolchain is what *produces* the artifact, by
        requiring javac and jar invocations in the build log for every
        configuration that installed something.  A submission whose jar appears
        without javac running has it from somewhere else, and the most likely
        somewhere is the source tree.
        """
        configs = self.installed_configs()
        if not configs:
            return False, "no configuration installed; nothing to attribute", []
        missing: list[str] = []
        found: list[str] = []
        for config in configs:
            outcome = self.outcomes[config]
            text = result_text(outcome.configure, outcome.compile, outcome.install)
            hits = sorted(set(re.findall(r"\b(javac|jar)\b", text)))
            if "javac" in hits:
                found.append(f"{config}: {hits}")
            else:
                missing.append(config)
        if missing:
            return (
                False,
                f"no javac invocation observed in the build of: {missing}; the jar "
                f"was not compiled by the graded build",
                found,
            )
        return True, "; ".join(found), []

    def gate_no_c_fallback(self) -> tuple[bool, str, list[str]]:
        """No C implementation retained behind a CMake option or build flag.

        G1's first gate already removes C sources, so what is left to catch is the
        machinery that *would* select one: an option named for a C or legacy path,
        a source directory holding the old implementation, or a build file that
        compiles something.  Any of those means the submission was written to keep
        both roads open, and the road not graded is the one that rots.

        The Java form adds one shape the Rust form had no equivalent for: a
        multi-release jar.  `META-INF/versions/` lets a jar ship different code per
        JDK, so a submission could pass every gate on the graded JDK and run
        something else anywhere later.  Only one JDK is graded, so this is the same
        "two roads" failure in a different spelling.
        """
        offenders: list[str] = []
        option_re = re.compile(
            r"""\b(?:option|cmake_dependent_option)\s*\(\s*
                ([A-Za-z0-9_]*(?:USE_C|WITH_C|C_IMPL|C_FALLBACK|LEGACY|NATIVE_C|
                 USE_ZLIB_C|PURE_C|CLASSIC|JNI)[A-Za-z0-9_]*)""",
            re.IGNORECASE | re.VERBOSE,
        )
        for path in self.files:
            name = path.name.lower()
            base = rel(self.repo, path)
            if name == "cmakelists.txt" or path.suffix == ".cmake":
                text = read_text(path)
                for match in option_re.finditer(text):
                    offenders.append(f"{base}: option {match.group(1)}")
                if re.search(r"\benable_language\s*\(\s*(?:C|CXX)\b", text):
                    offenders.append(
                        f"{base}: enable_language(C) keeps a C compiler in the build"
                    )
                if re.search(r"\badd_library\s*\(\s*[A-Za-z0-9_]+\s+[^)]*\.c\b", text):
                    offenders.append(f"{base}: an add_library still names a C source")
            if "META-INF/versions" in base.replace(os.sep, "/"):
                offenders.append(
                    f"{base}: a multi-release jar can ship different code per JDK"
                )
        if offenders:
            return (
                False,
                f"{len(offenders)} switch(es) that could select a second implementation",
                sorted(set(offenders))[:40],
            )
        return True, "no C-selecting option and no per-release source split", []


    # == G2: no C toolchain participated ===================================

    def gate_compiler_shim_clean(self) -> tuple[bool, str, list[str]]:
        """The shimmed drivers were never asked to compile a repository source.

        This is the gate the shim exists to make cheap.  A `reject` in the ledger
        is a compile the shim refused, which means the build still wanted to
        compile C; a `forward` carrying a `compile` verdict would mean the shim was
        reached but let one through, which it is written never to do, so that is
        graded as well rather than assumed impossible.

        The shim stays armed in the Java task for a reason worth stating: with no
        C left in the tree there is nothing for it to refuse, so a clean ledger is
        the build-time *fact* that the C is gone rather than a promise that it is.
        """
        rejects: list[str] = []
        leaked: list[str] = []
        seen = 0
        for config in sorted(self.outcomes):
            for event in self.shim_events(config):
                seen += 1
                kind = event.get("event")
                if kind == "reject":
                    rejects.append(
                        f"[{config}] {event.get('tool')} refused: "
                        f"{' '.join(event.get('sources') or ['(implicit)'])[:200]}"
                    )
                elif kind == "forward" and event.get("verdict") == "compile":
                    leaked.append(
                        f"[{config}] {event.get('tool')} compiled "
                        f"{' '.join(event.get('sources') or [])[:200]}"
                    )
        if rejects:
            return (
                False,
                f"the build attempted {len(rejects)} C/C++ compile(s), refused by the "
                f"shim; the submission still depends on compiling C",
                rejects[:30],
            )
        if leaked:
            return (
                False,
                f"{len(leaked)} compile action(s) reached a real compiler",
                leaked[:30],
            )
        if not seen:
            return True, "the shimmed drivers were never invoked", []
        return True, f"{seen} shim event(s), none of them a compile", []

    def gate_no_compile_syscalls(self) -> tuple[bool, str, list[str]]:
        """No C compiler back end was reached by a path around the shim.

        The shim owns the driver names on PATH, so the ways left to compile C are
        an absolute path to a compiler or a name the shim does not shadow.  Both
        leave the same fingerprint: a back-end binary named in the build output or
        in the generated rules.

        This gate is stricter than the Rust form could be, and the reason is the
        toolchain.  rustc reaches `as` and `ld` on every link, so the Rust form had
        to exclude both and lost the ability to see an assembler being run; a
        javac/jar build reaches no assembler and no linker at all, so naming either
        is itself a finding.  `collect2` is included on the same reasoning.
        """
        suspicious: list[str] = []
        backend = re.compile(
            r"(?:/usr(?:/local)?)?/(?:bin|lib(?:exec)?)/[\w./+-]*"
            r"(?:cc1plus|cc1|collect2|gcc-\d+|g\+\+-\d+|clang-\d+|tcc|icc)"
        )
        for config, outcome in sorted(self.outcomes.items()):
            for label, result in (
                ("configure", outcome.configure),
                ("build", outcome.compile),
                ("install", outcome.install),
            ):
                if result is None:
                    continue
                for match in backend.finditer(result_text(result)):
                    suspicious.append(f"[{config}] {label}: {match.group(0)}")
            for name in ("build.ninja", "Makefile", "CMakeCache.txt"):
                candidate = outcome.build_dir / name
                if not candidate.is_file():
                    continue
                for line in read_text(candidate).splitlines():
                    if "cc1plus" in line or re.search(r"\b(?:cc1|collect2)\b", line):
                        suspicious.append(f"[{config}] {name}: {line.strip()[:200]}")
        if suspicious:
            unique = sorted(set(suspicious))
            return (
                False,
                f"a C compiler back end was reached: {len(unique)} occurrence(s)",
                unique[:30],
            )
        return True, "no C compiler back end named in any build output", []

    def gate_no_prebuilt_objects(self) -> tuple[bool, str, list[str]]:
        """No compiled artifact is checked into the submitted source.

        Shipping a prebuilt zlib.jar and installing it satisfies every behavioral
        case with no migration having happened, so this closes the most direct
        shortcut.  Build directories are excluded -- artifacts there were produced
        by the build under observation -- and files whose suffix says nothing are
        sniffed, because dropping the suffix is the obvious way around a suffix
        list.

        The suffix list comes from the contract, which puts `.class`, `.jar` and
        `.jmod` beside the object-file extensions.  That is the substantive
        difference from the C-to-C form: there, a checked-in binary was a `.o` or a
        `.so`, and here it is a class file, which is smaller, more portable and
        easier to produce elsewhere.
        """
        exts = {e.lower() for e in self.contract["forbidden_paths"]["extensions"]}
        binary_exts = exts & {
            ".o", ".obj", ".a", ".lo", ".la", ".so", ".dylib", ".dll", ".jnilib",
            ".class", ".jar", ".jmod",
        }
        offenders: list[str] = []
        sniffed = 0
        for path in self.files:
            if path.suffix.lower() in binary_exts:
                offenders.append(rel(self.repo, path))
                continue
            if path.suffix.lower() in TEXT_SUFFIXES:
                continue
            if not path.is_file():
                continue
            sniffed += 1
            try:
                with path.open("rb") as handle:
                    head = handle.read(8)
            except OSError:
                continue
            for magic in BINARY_MAGICS:
                if head.startswith(magic):
                    offenders.append(
                        f"{rel(self.repo, path)} (compiled artifact, suffix says "
                        f"nothing)"
                    )
                    break
        if offenders:
            return (
                False,
                f"{len(offenders)} prebuilt binary artifact(s) checked into the "
                f"submission; the delivered jar must be built from source",
                sorted(offenders)[:40],
            )
        return (
            True,
            f"no prebuilt artifact among {len(self.files)} submitted files "
            f"({sniffed} sniffed by magic)",
            [],
        )

    def gate_no_embedded_reference(self) -> tuple[bool, str, list[str]]:
        """No compiled implementation is smuggled in as data.

        The C-to-C and C-to-Rust forms answered this by digesting the reference
        build and comparing.  That comparison does not exist here: the reference
        publishes `libz.so.1.3.1` and State B publishes a jar, and no digest
        relates them.  What replaces it is a scan for the three shapes a compiled
        Java implementation can arrive in without being a file named `.jar`:

          class bytes as data     a CAFEBABE header inside a file the tree calls
                                  something else, which `no-prebuilt-objects`
                                  sniffs for unsuffixed files and this one looks
                                  for everywhere, including inside text
          class bytes as a string a long Base64 or `\\u`-escaped literal, decoded
                                  and handed to defineClass at run time
          defineClass itself      the only way any of the above becomes runnable,
                                  so it is looked for in the source *and* in the
                                  delivered constant pool

        A limit worth stating plainly: a submission that took a pure-Java zlib
        port, renamed its packages and removed its copyright headers would leave
        none of these fingerprints, and no static gate in this file would catch it.
        That failure is what `no-vendored-zlib` is aimed at, and it is aimed with
        names rather than with structure, so it is defeated by thorough enough
        laundering.  The honest claim for this group is that it closes the
        mechanical shortcuts, not that it establishes authorship.
        """
        offenders: list[str] = []
        for path in self.files:
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            base = rel(self.repo, path)
            if size >= 32:
                try:
                    with path.open("rb") as handle:
                        blob = handle.read(4_000_000)
                except OSError:
                    blob = b""
                if MAGIC_CLASS in blob:
                    offenders.append(f"{base} contains class-file bytes")
                elif MAGIC_ZIP in blob and path.suffix.lower() not in (".md", ".txt"):
                    offenders.append(f"{base} contains an embedded archive")
            if path.suffix == ".java":
                text = read_text(path)
                if "defineClass" in text:
                    offenders.append(f"{base}: defineClass")
                for match in re.finditer(r'"([A-Za-z0-9+/=]{512,})"', text):
                    offenders.append(
                        f"{base}: a {len(match.group(1))}-character encoded literal"
                    )
        for config, cls in self.all_jar_classes():
            for owner, name, _ in cls.calls():
                if name == "defineClass":
                    offenders.append(
                        f"[{config}] {cls.binary_name} calls {owner}.defineClass"
                    )
        if offenders:
            return (
                False,
                "a compiled implementation is being carried as data rather than "
                "compiled from source",
                sorted(set(offenders))[:20],
            )
        return True, "no embedded class bytes and no defineClass", []

    def gate_no_vendored_zlib(self) -> tuple[bool, str, list[str]]:
        """No vendored or declared third-party compression implementation.

        Distinct from the source-closure gates: those ask whether *this* zlib's C
        is gone, and a submission can satisfy them while pulling in someone else's
        implementation instead.  The Java names matter more here than the C ones --
        jzlib is a pure-Java zlib port, would pass every provenance gate in G3, and
        is still not a migration of this repository.

        Three places are read.  The tree, for a vendored copy.  The build files,
        for a declared dependency: the policy is java.base only, so *any*
        dependency block is outside it, whatever it names.  And the delivered
        constant pool, because a name that survived into the artifact is stronger
        evidence than a name in a comment.
        """
        offenders: list[str] = []
        for path in self.files:
            name = path.name.lower()
            base = rel(self.repo, path)
            hits = foreign_markers(base)
            if hits:
                offenders.append(f"{base}: path names {hits}")
            if path.suffix.lower() in TEXT_SUFFIXES or name in BUILD_FILE_NAMES:
                text = read_text(path)
                hits = foreign_markers(text)
                if hits:
                    offenders.append(f"{base}: mentions {hits}")
            if name in ("pom.xml", "build.gradle", "build.gradle.kts", "ivy.xml"):
                text = read_text(path)
                for match in re.finditer(
                    r"<dependency>|^\s*(?:implementation|api|compileOnly|"
                    r"runtimeOnly|testImplementation)\s*[\s(]",
                    text, re.M,
                ):
                    offenders.append(
                        f"{base}: declares a dependency ({match.group(0).strip()[:40]}); "
                        f"the policy is java.base only"
                    )
                for match in re.finditer(
                    r"[\"']([a-z][a-z0-9._-]+:[a-z][a-z0-9._-]+:[0-9][^\"']*)[\"']", text
                ):
                    offenders.append(f"{base}: artifact coordinate {match.group(1)}")
        for config, cls in self.all_jar_classes():
            hits = foreign_markers(" ".join(cls.all_utf8()))
            if hits:
                offenders.append(
                    f"[{config}] {cls.binary_name} names {hits} in its constant pool"
                )
        if offenders:
            return (
                False,
                f"{len(offenders)} reference(s) to a third-party compression "
                f"implementation or an external dependency",
                sorted(set(offenders))[:40],
            )
        return True, "no vendored copy and no declared dependency", []


    # == G3: provenance of the shipped classes ==============================
    #
    # This is where the C-to-C and C-to-Rust forms read ELF: .comment strings for
    # which compiler produced an object, DWARF and symbol names for which
    # translation units went into it, the dynamic symbol table for what it imports.
    # A class file answers the same questions from different places -- the
    # major version for which toolchain, the SourceFile attribute for which source,
    # the constant pool for what it can reach -- and one of them answers *better*
    # than ELF does: every type a class touches is named in its constant pool
    # whether the touching code runs or not, so no arrangement of branches, lazy
    # initialization or exception handlers removes the entry.

    def gate_class_java_provenance(self) -> tuple[bool, str, list[str]]:
        """The delivered classes were compiled from Java by the graded JDK.

        Three facts, all read off the class files themselves.  The major version
        must be exactly the pinned one: 61 is Java 17, the only JDK on the image, so
        a class at 52 or 65 was produced somewhere else.  The minor version must be
        0, because a nonzero minor is the preview-features marker and preview
        bytecode is not a release artifact.  And every class must carry a SourceFile
        naming a `.java` file, which is what says a Java compiler compiled Java
        rather than a bytecode generator emitting whatever it liked.

        Requiring SourceFile is a real requirement, not an incidental one: javac
        writes it unless asked not to, so a submission that strips it with `-g:none`
        fails this gate and the task statement says so.  The alternative -- treating
        a missing attribute as acceptable -- would make the provenance question
        unanswerable for any submission that wanted it unanswerable.

        What this deliberately does not check is that the SourceFile matches the
        class's own simple name.  It reads as the obvious next tightening and it is
        wrong: a package-private helper declared alongside a public class in one file
        compiles to its own class file carrying the *enclosing file's* name, which is
        ordinary Java, so the tighter check would fail correct submissions over a
        stylistic choice the contract does not make.
        """
        want_major = int(self.expectations.major_version)
        want_minor = int(self.expectations.classfile_contract.get("minor_version", 0))
        offenders: list[str] = []
        counted = 0
        for config in self.installed_configs():
            classes = self.jar_classes(config)
            if not classes:
                offenders.append(f"[{config}] the installed jar holds no readable class")
                continue
            for cls in classes:
                counted += 1
                name = cls.binary_name
                if cls.major_version != want_major:
                    offenders.append(
                        f"[{config}] {name} is major {cls.major_version}, not "
                        f"{want_major}"
                    )
                if cls.minor_version != want_minor:
                    offenders.append(
                        f"[{config}] {name} has minor version {cls.minor_version}; "
                        f"nonzero means preview features"
                    )
                source = cls.source_file
                if not source:
                    offenders.append(
                        f"[{config}] {name} carries no SourceFile attribute; its "
                        f"provenance cannot be read"
                    )
                elif not source.endswith(".java"):
                    offenders.append(f"[{config}] {name} was compiled from {source!r}")
        if not counted and not offenders:
            return False, "no installed jar to inspect", []
        if offenders:
            return (
                False,
                "the delivered classes do not carry the graded toolchain's "
                "provenance",
                sorted(set(offenders))[:30],
            )
        return (
            True,
            f"{counted} class(es) at major {want_major}.{want_minor}, all from .java "
            f"sources",
            [],
        )

    def gate_no_c_provenance(self) -> tuple[bool, str, list[str]]:
        """No delivered class names a C translation unit as its origin.

        The complement of the previous gate, and it catches a different thing: a
        C-to-bytecode transliterator produces class files whose SourceFile is
        `deflate.c`, and a hand-written Java port never does.  The class file's own
        record of what it was compiled from is the cheapest possible statement of
        which language the work happened in.

        String constants are read too, but only the constants -- not every UTF-8
        entry.  A method named `inflate` is not evidence of anything, whereas the
        literal `"inftrees.c"` sitting in a delivered class is the C
        implementation's file name surviving into the artifact.
        """
        c_basenames = self.baseline_c_basenames()
        internal_headers = set(
            self.contract["forbidden_paths"]["internal_headers_that_must_be_removed"]
        )
        offenders: list[str] = []
        counted = 0
        for config, cls in self.all_jar_classes():
            counted += 1
            source = cls.source_file
            if source and Path(source).suffix.lower() in (
                ".c", ".cc", ".cpp", ".cxx", ".h", ".s", ".asm"
            ):
                offenders.append(
                    f"[{config}] {cls.binary_name} was compiled from {source!r}"
                )
            for literal in cls.string_literals():
                stripped = literal.strip()
                if stripped in c_basenames or stripped in internal_headers:
                    offenders.append(
                        f"[{config}] {cls.binary_name} carries the literal "
                        f"{stripped!r}"
                    )
        if not counted:
            return False, "no installed jar to inspect", []
        if offenders:
            return (
                False,
                "the delivered classes carry the C implementation's provenance",
                sorted(set(offenders))[:30],
            )
        return True, f"no C translation unit named by any of {counted} class(es)", []

    def gate_no_jdk_deflate(self) -> tuple[bool, str, list[str]]:
        """The artifact does not delegate to java.util.zip or java.util.jar.

        The central gate of this task.  Every other shortcut costs the submitter
        something -- a checked-in binary, a spawned process, a loaded library -- but
        the JDK ships a complete, correct, bit-compatible deflate in
        java.util.zip, three lines away and free.  A port that calls it produces the
        reference's exact bytes on every behavioural case in the catalog and has
        performed no migration whatsoever.  There is no behavioral test that
        separates the two, which is why this is a mandatory gate rather than a
        scored case.

        Checked twice, in the two places the answer can be.

        Statically, in the constant pool: every type a class touches is named
        there, and the string constants are read as well because `Class.forName`
        with a name assembled from a literal is what a reflective call looks like.
        Split literals defeat the string half, which is why the dynamic half exists.

        Dynamically, by class loading: the JVM is asked to log every class it loads
        during a compression round trip, and the same list is taken from a run that
        loads the same classes out of the same jar and compresses nothing.  What is
        left after subtracting is what compression itself needed.  A reflective load
        appears there under its real name however the name was assembled, because
        the JVM logs the class it actually loaded.

        The dynamic half is scoped to `java.util.zip` and `java.util.jar` rather
        than to the whole forbidden-prefix list, and the reason is measurement
        rather than leniency.  `sun.` and `jdk.internal.` classes load in their
        hundreds during JVM startup and continue loading lazily afterwards, so a
        differential over those prefixes reports timing noise; a `java.util.zip`
        class arriving after the baseline already read four jar entries is not
        noise.  The prefixes the differential drops are exactly the ones the static
        scan reads precisely, so nothing goes ungraded.
        """
        prefixes = tuple(
            self.expectations.classfile_contract.get(
                "forbidden_prefixes", FORBIDDEN_PREFIXES_DEFAULT
            )
        )
        forbidden_types = set(
            self.expectations.classfile_contract.get("forbidden_type_references", ())
        )
        forbidden_strings = set(
            self.expectations.classfile_contract.get("forbidden_string_constants", ())
        )
        offenders: list[str] = []
        counted = 0
        for config, cls in self.all_jar_classes():
            counted += 1
            name = cls.binary_name
            for referenced in sorted(cls.referenced_types()):
                hit = classfile.matches_prefix(referenced, prefixes)
                if hit is not None:
                    offenders.append(f"[{config}] {name} references {referenced}")
                elif referenced in forbidden_types:
                    offenders.append(f"[{config}] {name} references {referenced}")
            for literal in sorted(cls.string_literals()):
                if literal in forbidden_strings:
                    offenders.append(
                        f"[{config}] {name} carries the literal {literal!r}"
                    )
                elif classfile.matches_prefix(literal.replace(".", "/"), prefixes):
                    offenders.append(
                        f"[{config}] {name} carries the literal {literal!r}"
                    )
        if not counted:
            return False, "no installed jar to inspect", []
        if offenders:
            return (
                False,
                f"the delivered classes reach into {_reached_into(offenders)}; "
                f"the port must be the submission's code",
                sorted(set(offenders))[:30],
            )

        dynamic_prefixes = ("java.util.zip.", "java.util.jar.")
        observed: list[str] = []
        for config in self.installed_configs():
            pair = self._differential(config)
            if pair is None:
                measured = self._observe(config, "roundtrip")
                baseline = self._observe(config, "noop")
                detail = measured.detail if not measured.ok else baseline.detail
                return (
                    False,
                    f"[{config}] the class-load differential could not be taken: "
                    f"{detail}",
                    [],
                )
            measured, baseline = pair
            extra = sorted(measured.classes - baseline.classes)
            hits = [n for n in extra if n.startswith(dynamic_prefixes)]
            if hits:
                return (
                    False,
                    f"[{config}] compression loaded {len(hits)} JDK compression "
                    f"class(es) that a run compressing nothing did not",
                    hits[:20],
                )
            observed.append(
                f"[{config}] {len(extra)} class(es) loaded by compression, none "
                f"from java.util.zip or java.util.jar"
            )
        if not observed:
            return False, "no configuration could be observed", []
        return (
            True,
            f"{counted} class(es) name no forbidden type; " + "; ".join(observed),
            [],
        )

    def gate_no_native_methods(self) -> tuple[bool, str, list[str]]:
        """No native method, and no native library in the jar.

        A native method means machine code, and machine code means the C is still
        there -- behind a JNI boundary instead of behind a header.  ACC_NATIVE is a
        flag in the method's own access bits, so this cannot be hidden by any
        arrangement of the code that declares it.

        The jar's entry names are read as well.  A bundled `.so` is useless without
        a native method to reach it, so finding one means either a native method
        somewhere this gate did not look or a payload waiting to be loaded, and both
        are the same finding.
        """
        if not self.contract["classfile_contract"].get("forbidden_native_methods", True):
            return True, "the contract does not forbid native methods", []
        offenders: list[str] = []
        counted = 0
        for config, cls in self.all_jar_classes():
            counted += 1
            if cls.has_native_method:
                offenders.append(
                    f"[{config}] {cls.binary_name} declares native "
                    f"{cls.native_methods()}"
                )
        for config in self.installed_configs():
            handle = self.jar_handle(config)
            if handle is None:
                continue
            for entry in handle.names():
                if entry.lower().endswith((".so", ".dll", ".dylib", ".jnilib")):
                    offenders.append(f"[{config}] the jar bundles {entry}")
        if not counted:
            return False, "no installed jar to inspect", []
        if offenders:
            return (
                False,
                "the artifact reaches machine code; the port must be Java",
                sorted(set(offenders))[:20],
            )
        return True, f"no native method among {counted} class(es), no bundled library", []

    def gate_no_unsafe_ffm(self) -> tuple[bool, str, list[str]]:
        """No sun.misc.Unsafe and no java.lang.foreign downcall.

        Both are the FFI route back to a system libz, and both are also how a
        submission would reach the JDK's internals after the package scan closed
        the front door.  `java.lang.foreign` is the one that matters most: a
        `Linker.downcallHandle` for `deflate` is a JNI call without a native
        method, and it would satisfy every gate that reads ACC_NATIVE.

        The build's own flags are read too.  Foreign memory access on 17 is a
        preview API and needs `--enable-preview`; reaching jdk.internal needs
        `--add-exports` or `--add-opens`.  A build that passes any of those is
        arranging for something this gate exists to prevent, whether or not the
        delivered classes ended up using it.
        """
        needles = (
            "sun/misc/Unsafe", "jdk/internal/misc/Unsafe", "java/lang/foreign/",
        )
        offenders: list[str] = []
        counted = 0
        for config, cls in self.all_jar_classes():
            counted += 1
            for referenced in sorted(cls.referenced_types()):
                if any(referenced.startswith(n) for n in needles):
                    offenders.append(
                        f"[{config}] {cls.binary_name} references {referenced}"
                    )
            for literal in sorted(cls.string_literals()):
                slashed = literal.replace(".", "/")
                if any(slashed.startswith(n) for n in needles):
                    offenders.append(
                        f"[{config}] {cls.binary_name} names {literal!r}"
                    )
        for config, outcome in sorted(self.outcomes.items()):
            text = result_text(outcome.configure, outcome.compile, outcome.install)
            for match in re.finditer(
                r"--(?:enable-preview|enable-native-access|add-exports|add-opens|"
                r"illegal-access)\S*", text
            ):
                offenders.append(f"[{config}] the build passes {match.group(0)}")
        if not counted:
            return False, "no installed jar to inspect", []
        if offenders:
            return (
                False,
                "an FFI or internal-access route is present; the implementation "
                "must be ordinary Java",
                sorted(set(offenders))[:20],
            )
        return True, f"no Unsafe and no foreign linker among {counted} class(es)", []

    def gate_deps_whitelist(self) -> tuple[bool, str, list[str]]:
        """Nothing outside java.base and the submission's own packages.

        The Java counterpart of the C-to-C form's DT_NEEDED gate, and stricter than
        it, because a class file names every type it can reach rather than only the
        libraries it links.  What java.base exports is asked of the grading JVM
        rather than listed here: a hardcoded list would be a second copy of the
        JDK's module descriptor, silently drifting, and the gate is only as good as
        its idea of what java.base contains.

        Three readings.  Every referenced type must resolve to java.base or to the
        submission's own packages.  The module descriptor must require nothing else.
        And the manifest must declare no Class-Path, which is the one way a jar can
        pull in another jar without naming it in any class file.
        """
        allowed = self.base_packages()
        if allowed is None:
            return (
                False,
                "the grading JVM would not describe java.base, so the dependency "
                "surface could not be established",
                [],
            )
        forbidden_requires = set(
            self.expectations.module_contract.get("forbidden_requires", ())
        )
        want_requires = set(self.expectations.module_contract.get("requires", ()))
        offenders: list[str] = []
        counted = 0
        for config in self.installed_configs():
            own = self.submission_packages(config)
            for cls in self.jar_classes(config):
                counted += 1
                for referenced in sorted(cls.referenced_types()):
                    if "/" not in referenced:
                        # The default package: either the submission's own or a
                        # primitive-holder name, and neither is a dependency.
                        continue
                    package = referenced.rsplit("/", 1)[0] + "/"
                    if package in allowed or package in own:
                        continue
                    offenders.append(
                        f"[{config}] {cls.binary_name} references {referenced}, "
                        f"which is outside java.base"
                    )
            handle = self.jar_handle(config)
            if handle is None:
                continue
            descriptor = handle.module_descriptor()
            if descriptor is not None:
                required = descriptor.required_modules()
                extra = sorted(required - want_requires - {"java.base"})
                if extra:
                    offenders.append(
                        f"[{config}] module-info requires {extra} beyond "
                        f"{sorted(want_requires)}"
                    )
                for name in sorted(required & forbidden_requires):
                    offenders.append(f"[{config}] module-info requires {name}")
            manifest = handle.manifest()
            if manifest.get("Class-Path"):
                offenders.append(
                    f"[{config}] the manifest declares Class-Path "
                    f"{manifest['Class-Path']!r}, which pulls in another artifact"
                )
        if not counted:
            return False, "no installed jar to inspect", []
        if offenders:
            return (
                False,
                f"{len(offenders)} dependency(ies) outside java.base",
                sorted(set(offenders))[:30],
            )
        return (
            True,
            f"{counted} class(es) reach nothing outside java.base "
            f"({len(allowed)} exported packages) and the submission's own",
            [],
        )

    def gate_no_system_zlib(self) -> tuple[bool, str, list[str]]:
        """The artifact does not reach the system zlib by any route.

        Four routes, and all four are read.  `System.loadLibrary("z")` is the
        obvious one and is a constant-pool fact.  An absolute path to a `libz.so`
        as a string literal is the same thing spelled differently.  A build that
        links or copies a system library shows in the build output.  And a run that
        opened one shows in the interposer's ledger, which is what catches a route
        assembled at run time from pieces no scan would recognize.

        The runtime half is why `java.library.path` is pinned empty on every JVM
        the verifier starts: with no library path, `loadLibrary("z")` finds nothing
        to load, so the attempt fails at the point of the attempt rather than
        succeeding quietly on an image that happens to ship libz.
        """
        offenders: list[str] = []
        counted = 0
        library_re = re.compile(r"(?:^|/)libz\.(?:so|a|dylib)(?:\.\d+)*$|^z$")
        for config, cls in self.all_jar_classes():
            counted += 1
            for owner, name, _ in cls.calls():
                if name in ("load", "loadLibrary") and owner in (
                    "java.lang.System", "java.lang.Runtime", "java.lang.ClassLoader"
                ):
                    offenders.append(
                        f"[{config}] {cls.binary_name} calls {owner}.{name}"
                    )
            for literal in sorted(cls.string_literals()):
                if library_re.search(literal.strip()):
                    offenders.append(
                        f"[{config}] {cls.binary_name} names the library "
                        f"{literal!r}"
                    )
        for config, outcome in sorted(self.outcomes.items()):
            text = result_text(outcome.configure, outcome.compile, outcome.install)
            for match in re.finditer(r"-lz\b|libz\.so(?:\.\d+)*|\bfind_package\s*\(\s*ZLIB",
                                     text):
                offenders.append(f"[{config}] the build mentions {match.group(0)}")
        observed: list[str] = []
        for config in self.installed_configs():
            pair = self._differential(config)
            if pair is None:
                continue
            measured, _ = pair
            for entry in measured.files:
                if "libz" in entry or entry.rstrip().endswith(".so"):
                    offenders.append(f"[{config}] the round trip opened {entry}")
            for program in measured.execs:
                base = Path(program).name
                if base in ("gzip", "gunzip", "zcat", "pigz", "zlib", "compress"):
                    offenders.append(f"[{config}] the round trip executed {program}")
            observed.append(f"[{config}] observed")
        if not counted:
            return False, "no installed jar to inspect", []
        if offenders:
            return (
                False,
                "the artifact can reach a system zlib; the implementation must be "
                "the submission's own code",
                sorted(set(offenders))[:20],
            )
        if not observed:
            return False, "no round trip could be observed to confirm it at run time", []
        return (
            True,
            f"no library load, no system library named, and none opened at run time "
            f"({'; '.join(observed)})",
            [],
        )

    def gate_no_library_load(self) -> tuple[bool, str, list[str]]:
        """No System.load, loadLibrary or findLibrary of anything at all.

        Broader than the previous gate and narrower in what it reads: that one asks
        whether the *system zlib* is reachable, this one asks whether any native
        library is loaded, whatever it is called.  A submission that ships its own
        `libfast.so` and loads it has moved the C rather than removed it, and the
        name gives no clue.

        `ClassLoader.findLibrary` is included because it is the override point: a
        custom loader that answers it can serve a library from inside the jar, which
        is the one route that leaves no absolute path anywhere.
        """
        offenders: list[str] = []
        counted = 0
        for config, cls in self.all_jar_classes():
            counted += 1
            for owner, name, _ in cls.calls():
                if name in ("load", "loadLibrary", "load0", "loadLibrary0",
                            "findLibrary", "mapLibraryName"):
                    if owner.startswith("java.lang.") or owner.endswith("ClassLoader"):
                        offenders.append(
                            f"[{config}] {cls.binary_name} calls {owner}.{name}"
                        )
        for path in self.java_files:
            text = read_text(path)
            for match in re.finditer(
                r"System\.load(?:Library)?\s*\(|Runtime\.getRuntime\s*\(\s*\)\s*\."
                r"load(?:Library)?\s*\(", text
            ):
                offenders.append(f"{rel(self.repo, path)}: {match.group(0).strip()}")
        if not counted:
            return False, "no installed jar to inspect", []
        if offenders:
            return (
                False,
                "the artifact loads a native library; every byte it executes must be "
                "Java compiled from the submission",
                sorted(set(offenders))[:20],
            )
        return True, f"no library load among {counted} class(es)", []

    def gate_no_exec_helpers(self) -> tuple[bool, str, list[str]]:
        """The library spawns no helper process while under test.

        Handing the work to `gzip` would satisfy every byte-exactness case in the
        catalog, so this is the same class of shortcut as calling java.util.zip and
        it is graded the same way: statically where the answer is exact, dynamically
        where a scan can be evaded.

        The static half is the constant pool -- `ProcessBuilder`, `Runtime` and
        `ProcessHandle` are all forbidden type references in the contract.  The
        dynamic half is the interposer's exec ledger, which is the Java form's
        advance over the Rust form: there, the evidence was an import of `execve` in
        the symbol table, which said the *capability* was linked in, not that it was
        used.  Here the ledger says whether a process was actually spawned inside the
        measured window.
        """
        needles = (
            "java/lang/ProcessBuilder", "java/lang/Runtime", "java/lang/Process",
            "java/lang/ProcessHandle",
        )
        offenders: list[str] = []
        counted = 0
        for config, cls in self.all_jar_classes():
            counted += 1
            for referenced in sorted(cls.referenced_types()):
                if referenced in needles or referenced.startswith(
                    "java/lang/ProcessBuilder$"
                ):
                    offenders.append(
                        f"[{config}] {cls.binary_name} references {referenced}"
                    )
        if not counted:
            return False, "no installed jar to inspect", []
        observed: list[str] = []
        for config in self.installed_configs():
            pair = self._differential(config)
            if pair is None:
                # Whichever half failed is the one worth naming.  Reporting the
                # measured run's detail unconditionally once described a perfectly
                # good measurement as the reason nothing could be observed.
                measured = self._observe(config, "roundtrip")
                baseline = self._observe(config, "noop")
                detail = measured.detail if not measured.ok else baseline.detail
                return (
                    False,
                    f"[{config}] process spawning could not be observed: {detail}",
                    [],
                )
            measured, _ = pair
            if measured.execs:
                offenders.append(
                    f"[{config}] the measured window spawned "
                    f"{len(measured.execs)} process(es): {measured.execs[:4]}"
                )
            else:
                observed.append(f"[{config}] no exec in the measured window")
        if offenders:
            return (
                False,
                "the library hands work to another program, which is where the "
                "compression would then be happening",
                sorted(set(offenders))[:20],
            )
        return (
            True,
            f"no process API among {counted} class(es); " + "; ".join(observed),
            [],
        )

    def gate_no_file_access(self) -> tuple[bool, str, list[str]]:
        """The compression paths read no file outside the caller's buffers.

        The gzip surface legitimately opens files, so the observation is scoped to
        an in-memory round trip: the observer deflates and inflates buffers and
        touches nothing on disk.  A submission that has memoized answers in a data
        file, or that shells out to a compressor, has to open something to do it,
        and this is where that shows.

        Two mechanisms make this answerable on a JVM, which is the substantive
        difference from the C-to-C form.  A JVM opens several hundred files before
        it runs a line of submitted code -- the modules image, the jar, locale data,
        /proc for its own accounting -- so the ledger is bracketed by two marker
        opens and only what happened between them is read.  And the round trip is
        warmed before the window opens, so class loading, which legitimately reads
        the jar, has finished by the time the measurement starts.

        Interposition rather than ptrace: Docker's default seccomp profile does not
        reliably permit PTRACE_ATTACH inside a grading container, and a gate that
        cannot run fails closed -- so a mechanism that needs no privileges is the
        difference between a gate and a permanent failure.  Interposition rather
        than a Java-side observer for a stronger reason: a SecurityManager, an
        agent or a wrapped FileSystemProvider runs in the same JVM as the submission
        and can be uninstalled by it in one call.
        """
        configs = self.installed_configs()
        if not configs:
            return False, "no configuration installed; nothing to observe", []
        offenders: list[str] = []
        evidence: list[str] = []
        for config in configs:
            observation = self._observe(config, "roundtrip")
            if not observation.ok:
                offenders.append(f"[{config}] {observation.detail}")
                continue
            if observation.files:
                offenders.append(
                    f"[{config}] the round trip opened {len(observation.files)} "
                    f"file(s): {sorted(set(observation.files))[:6]}"
                )
            else:
                evidence.append(f"[{config}] the in-memory round trip opened no file")
        if offenders:
            return (
                False,
                "the compression paths reach the filesystem, which is where a "
                "memoized answer or an external compressor would have to live",
                offenders[:20],
            )
        if not evidence:
            return False, "the round trip could not be observed in any configuration", []
        return True, "; ".join(evidence), []


    # == G4: one implementation, on the default path ========================

    def gate_single_implementation(self) -> tuple[bool, str, list[str]]:
        """Exactly one jar per configuration, with no spare engine beside it.

        The install tree is the statement of what was delivered.  A second jar
        there -- zlib-c.jar, zlib-fast.jar, zlib.orig.jar -- means the submission
        shipped a choice rather than an implementation, and whichever one the
        grader does not put on the module path is the one that was never migrated.

        One real file and one symlink is the expected layout: upstream's install
        rules deliver a versioned artifact with an unversioned alias, and the Java
        form keeps that shape (`zlib-1.3.1.jar` with `zlib.jar` pointing at it).
        Only additional *files* are graded here, because requiring the symlink is a
        separate structural case.

        The source tree is read as well, and for a reason the C form did not have:
        a jar checked into the tree is not merely a spare engine, it is a
        prebuilt one, and a build that copies it into place produces an artifact
        that passes every behavioural case without compiling anything.  The
        prebuilt-objects gate reads the same fact from the other direction; both
        report it because a submission that trips one has an explanation to give.
        """
        offenders: list[str] = []
        counted: list[str] = []
        for config in self.installed_configs():
            share = self.outcomes[config].prefix / "share" / "java"
            if not share.is_dir():
                continue
            reals: list[str] = []
            links: list[str] = []
            for entry in sorted(share.iterdir()):
                if entry.is_symlink():
                    links.append(entry.name)
                    continue
                if not entry.is_file():
                    continue
                reals.append(entry.name)
            expected = {Path(self.expectations.jar_relpath).name}
            extra = sorted(set(reals) - expected)
            if extra:
                offenders.append(f"[{config}] share/java also holds {extra}")
            counted.append(f"[{config}] {sorted(reals)} + {sorted(links)}")
        for path in self.files:
            if path.suffix.lower() in (".jar", ".jmod", ".war", ".ear"):
                offenders.append(
                    f"{rel(self.repo, path)} is a prebuilt archive in the source tree"
                )
        if offenders:
            return (
                False,
                "more than one compression implementation is delivered",
                offenders[:20],
            )
        if not counted:
            # This gate reads share/java directly rather than through jar(), because
            # what it counts is *neighbours* of the artifact and not the artifact.
            # So when it finds nothing it has to ask jar() explicitly, or the one
            # gate whose subject is "how many deliveries are there" is the one gate
            # that fails without recording that there were none.
            for config in self.installed_configs():
                self.jar(config)
            return False, "nothing was installed; there is no delivery to inspect", []
        return True, "; ".join(counted), []

    def gate_no_env_dispatch(self) -> tuple[bool, str, list[str]]:
        """Behavior does not change with the environment.

        Two halves, and the second is the one that matters.  Reading the Java for
        `System.getenv` finds the obvious spelling; running the library under a
        hostile environment and comparing the bytes finds the rest, whatever it was
        spelled like.  The environment used is deliberately the one a submission
        would reach for if it wanted to know it was being graded.

        The static half reads the constant pool as well as the source, which is
        what makes it hard to spell around: `getenv` reaches `java.lang.System` and
        the member reference is in the pool of the class that calls it whether the
        call site was written as `System.getenv("X")`, assembled reflectively, or
        hidden behind a helper.

        `System.getProperty` is graded with `getenv` because on a JVM it is the same
        capability with a different name -- `-DZLIB_FAST=1` on the command line and
        `ZLIB_FAST=1` in the environment both reach a submission that asks.  The
        one exception is the small set of properties the verifier itself pins
        (`file.encoding`, `user.language` and the rest), which a correct
        implementation has no reason to read either; they are not exempted, because
        a compression library that branches on the default charset has a bug rather
        than a defence.

        The source half reads the implementation sources and not the test drivers,
        which is the one place in this phase that distinction is drawn.  A C
        `main()` was handed the name it was invoked under, and upstream's minigzip
        used it twice: to prefix its error messages, and to decide from its own
        basename whether it had been called as gunzip or zcat.  A JVM `main` is
        handed the arguments without the name, so a faithful port has to be told --
        and a property is how the launcher tells it.  Refusing that would be
        grading the JVM's calling convention as if it were a submission's defence.
        The artifact half is unaffected: driver classes are forbidden in the jar, so
        `all_jar_classes` never sees them, and it is the artifact that runs.
        """
        offenders: list[str] = []
        for path in self.implementation_java_files:
            text = read_text(path)
            for match in re.finditer(
                r"System\s*\.\s*getenv\b|System\s*\.\s*getProperty\b|"
                r"ProcessEnvironment|System\s*\.\s*getProperties\b", text
            ):
                offenders.append(f"{rel(self.repo, path)}: {match.group(0).strip()}")
        for config, cls in self.all_jar_classes():
            for owner, name, _ in cls.calls():
                if owner == "java.lang.System" and name in (
                    "getenv", "getProperty", "getProperties"
                ):
                    offenders.append(
                        f"[{config}] {cls.binary_name} calls {owner}.{name}"
                    )
        if offenders:
            return (
                False,
                f"{len(offenders)} environment read(s) in the implementation; the "
                f"library's output must not depend on the environment",
                sorted(set(offenders))[:20],
            )
        configs = self.installed_configs()
        if not configs:
            return False, "no configuration installed; nothing to compare", []
        hostile = {
            "ZLIB_TEST": "1", "SWEREFACTOR": "1", "CI": "true", "TESTING": "1",
            "ZLIB_LEVEL": "1", "ZLIB_IMPL": "c", "ZLIB_FAST": "1",
            "NDEBUG": "1", "DEBUG": "1", "JAVA_TOOL_OPTIONS": "",
        }
        differing: list[str] = []
        checked: list[str] = []
        for config in configs:
            baseline = self._observe_env(config, {})
            if baseline is None:
                differing.append(f"[{config}] the round trip could not be run")
                continue
            loaded = self._observe_env(config, hostile)
            if loaded is None:
                differing.append(
                    f"[{config}] the round trip failed under a loaded environment"
                )
                continue
            if baseline != loaded:
                differing.append(
                    f"[{config}] output changed with the environment: "
                    f"{baseline[:80]!r} vs {loaded[:80]!r}"
                )
            else:
                checked.append(f"[{config}] identical under {len(hostile)} variables")
        if differing:
            return False, "the library's behavior depends on the environment", differing[:10]
        return True, "; ".join(checked), []

    def gate_no_verifier_awareness(self) -> tuple[bool, str, list[str]]:
        """No branch keyed on the verifier's paths, names or artifacts.

        A submission that recognizes the harness can behave one way under test and
        another in production, which is the failure mode this whole group exists
        to prevent.  Two lists, because the author was given one of these names and
        not the others.

        VERIFIER_AWARENESS_PATTERNS names things no document the author holds
        contains -- the shim's variables, the corpus blob paths, the instrument
        class names.  A mention is the finding, comments included: there is nowhere
        legitimate to have learned them.

        CONTRACT_PATH_PATTERNS is `/opt/swerefactor/source-contract.json`, which
        instruction.md names as the normative API statement and tells the author to
        read.  Citing it in a comment is following the instruction, so for these a
        use is the finding and a mention is not.

        Read from the delivered constant pool as well as from the source, which is
        the Java form's addition.  A build that generates a class from a template
        can put a string in the artifact that appears in no `.java` file in the
        tree, and the artifact is what runs.  The constant pool takes both lists
        flatly, since a class file has no comments to exempt.
        """
        offenders: list[str] = []
        for path in self.files:
            if (
                path.suffix.lower() not in TEXT_SUFFIXES
                and path.name.lower() not in BUILD_FILE_NAMES
            ):
                continue
            text = read_text(path)
            for pattern in VERIFIER_AWARENESS_PATTERNS:
                for match in re.finditer(pattern, text, re.IGNORECASE):
                    offenders.append(
                        f"{rel(self.repo, path)}: matches {pattern!r} "
                        f"({match.group(0)[:60]!r})"
                    )
            for pattern in CONTRACT_PATH_PATTERNS:
                for match in re.finditer(pattern, text, re.IGNORECASE):
                    if in_comment(text, match.start()):
                        continue
                    line = text.count("\n", 0, match.start()) + 1
                    offenders.append(
                        f"{rel(self.repo, path)}:{line}: matches {pattern!r} "
                        f"({match.group(0)[:60]!r}) outside a comment"
                    )
        # The artifact half takes both lists with no exemption.  A class file has no
        # comments left in it, so every string here is one the code can act on.
        for config, cls in self.all_jar_classes():
            for literal in sorted(cls.string_literals()):
                for pattern in VERIFIER_AWARENESS_PATTERNS + CONTRACT_PATH_PATTERNS:
                    if re.search(pattern, literal, re.IGNORECASE):
                        offenders.append(
                            f"[{config}] {cls.binary_name} carries {literal[:60]!r}, "
                            f"which matches {pattern!r}"
                        )
        if offenders:
            return (
                False,
                f"{len(offenders)} reference(s) to the verifier's own machinery; the "
                f"implementation must not know it is being graded",
                sorted(set(offenders))[:30],
            )
        return True, "no reference to the verifier in the source or the artifact", []

    def gate_default_path(self) -> tuple[bool, str, list[str]]:
        """The graded artifacts are what a default build produces.

        Both configurations are run with nothing but the documented flags, so
        anything the submission needs beyond that -- a required option, an
        environment variable set in a wrapper, a non-default preset -- shows up as
        a build that did not produce the artifacts.  This gate is therefore mostly
        a restatement in the audit report of what the build already proved,
        and it exists so the report says *why* a submission with a working
        non-default build still fails.

        Three things are reported even when the build worked, because each is how a
        non-default path gets smuggled in: a CMake preset file, a wrapper script
        named like the build, and a Gradle or Maven wrapper.  The last is the one
        specific to this task -- `gradlew` downloads a distribution on first use,
        which cannot work offline, so its presence means the submission was
        developed against a build path the grader will never take.
        """
        missing: list[str] = []
        for config in sorted(self.outcomes):
            outcome = self.outcomes[config]
            if not outcome.configured:
                missing.append(f"[{config}] the default configure failed")
                continue
            if not outcome.built:
                missing.append(f"[{config}] the default build failed")
                continue
            if not outcome.installed:
                missing.append(f"[{config}] the default install failed")
                continue
            jar = self.jar(config)
            if jar is None:
                missing.append(
                    f"[{config}] the default build did not deliver "
                    f"{self.expectations.jar_relpath}"
                )
        for path in self.files:
            name = path.name.lower()
            if name in ("cmakepresets.json", "cmakeuserpresets.json"):
                missing.append(f"{rel(self.repo, path)} introduces a non-default preset")
            if name in ("gradlew", "gradlew.bat", "mvnw", "mvnw.bat"):
                missing.append(
                    f"{rel(self.repo, path)} is a build-tool wrapper that fetches a "
                    f"distribution; the graded build is offline cmake"
                )
        if missing:
            return False, "the artifacts are not what a default build produces", missing[:20]
        return (
            True,
            f"{len(self.installed_configs())} configuration(s) delivered the jar with "
            f"default flags",
            [],
        )

    def gate_no_network(self) -> tuple[bool, str, list[str]]:
        """The build performs no network access.

        The container has no route, so a build that needs the network fails rather
        than succeeding quietly -- which makes this gate's job to explain such a
        failure, and to catch the declarations that would have needed it.

        What it reads is specific to the JVM ecosystem, where fetching a dependency
        at build time is the normal way to work rather than an exception: a Maven or
        Gradle declaration, a repository URL, a wrapper distribution, an Ivy or
        Gradle cache path.  Any of those means the submission's build is not the
        offline one being graded, whether or not the network was reached this time.
        """
        offenders: list[str] = []
        for config, outcome in sorted(self.outcomes.items()):
            text = result_text(outcome.configure, outcome.compile, outcome.install)
            for pattern in (
                r"Downloading from ",
                r"Could not resolve host",
                r"[Nn]etwork is unreachable",
                r"Connection refused",
                r"repo\.maven\.apache\.org",
                r"plugins\.gradle\.org",
                r"services\.gradle\.org",
                r"failed to (?:fetch|download)",
            ):
                for match in re.finditer(pattern, text):
                    offenders.append(f"[{config}] build output: {match.group(0)}")
        for path in self.files:
            name = path.name.lower()
            if name in ("pom.xml", "build.gradle", "build.gradle.kts",
                        "settings.gradle", "settings.gradle.kts", "ivy.xml",
                        "gradle-wrapper.properties", "maven-wrapper.properties"):
                text = read_text(path)
                for match in re.finditer(
                    r"https?://[^\s\"'<>]+|distributionUrl\s*=\s*\S+", text
                ):
                    offenders.append(
                        f"{rel(self.repo, path)}: {match.group(0).strip()[:80]}"
                    )
            if path.suffix in (".sh", ".cmake") or path.name.lower() == "cmakelists.txt":
                text = read_text(path)
                for match in re.finditer(
                    r"\b(?:curl|wget|git\s+clone|FetchContent_Declare|"
                    r"ExternalProject_Add|file\s*\(\s*DOWNLOAD)\b", text
                ):
                    offenders.append(f"{rel(self.repo, path)}: {match.group(0)}")
        if offenders:
            return (
                False,
                f"{len(offenders)} sign(s) of network access in the build",
                sorted(set(offenders))[:20],
            )
        return True, "no network access declared or attempted", []

    def gate_no_corpus_answers(self) -> tuple[bool, str, list[str]]:
        """No table of expected outputs keyed by input digest or length.

        The shortcut this closes: hash the input, look up the deflate stream the
        reference would have produced, return it.  Every byte-exact case passes and
        nothing was implemented.  Hashing appears in the patterns because a lookup
        needs a key, and a zlib implementation has no legitimate reason to compute
        SHA-256 or MD5 -- adler32 and crc32 are its own business and are not in the
        list.

        The Java-specific half is resource loading.  A rewrite legitimately needs
        tables -- the fixed Huffman codes, the length and distance bases -- and
        those are small and belong in source as arrays.  Reaching
        `getResourceAsStream` for them is the shape a smuggled answer table has, and
        it is the one route that survives every path-based check, because the
        resource travels inside the jar rather than beside it.  Serialization is
        read for the same reason: an `ObjectInputStream` over a bundled resource is
        a lookup table with extra steps.

        The oversized-tables gate grades the size question separately, so a
        submission with a defensible resource still has to explain itself here.
        """
        offenders: list[str] = []
        for path in self.java_files:
            text = read_text(path)
            for pattern in MEMOIZATION_PATTERNS:
                for match in re.finditer(pattern, text):
                    offenders.append(f"{rel(self.repo, path)}: {match.group(0)}")
        for config in self.installed_configs():
            handle = self.jar_handle(config)
            if handle is None:
                continue
            for entry in handle.names():
                if entry.endswith("/") or entry.endswith(".class"):
                    continue
                if entry.startswith("META-INF/"):
                    continue
                offenders.append(
                    f"[{config}] the jar bundles the resource {entry}, which is where "
                    f"a stored answer table would travel"
                )
        if offenders:
            return (
                False,
                f"{len(offenders)} sign(s) of digest-keyed memoization or a bundled "
                f"answer table",
                sorted(set(offenders))[:20],
            )
        return True, "no digest-keyed lookup and no bundled resource", []

    def _table_budget(self) -> int:
        """The per-file size ceiling, derived from State A's own largest table.

        A fixed number would be a guess.  State A ships crc32.h -- 578 KiB of
        generated CRC braid tables -- and a faithful Java port of that file is the
        same order of magnitude, so any ceiling below it would fail honest work.
        Taking the baseline's largest source file and doubling it states the
        reasoning instead of hiding it: whatever the biggest table upstream needed,
        twice that is room enough.

        The headroom is safe because the thing being excluded is much larger.  The
        behavioural corpus is several megabytes of payloads compressed at ten levels
        with several strategies; the smallest stored-answer table that could satisfy
        the suite is tens of megabytes, more than an order of magnitude above this
        ceiling.
        """
        largest = 0
        for path in self.baseline.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() in (".pdf", ".txt", ".3"):
                continue
            try:
                largest = max(largest, path.stat().st_size)
            except OSError:
                continue
        return max(largest * 2, 256 * 1024)

    def gate_no_oversized_tables(self) -> tuple[bool, str, list[str]]:
        """No data blob large enough to hold precomputed answers.

        A budget rather than a ban, because zlib genuinely ships tables: crc32.h is
        the generated CRC braid table and inffixed.h is the fixed Huffman table, and
        a faithful rewrite reproduces both.  What a rewrite does not need is tens of
        megabytes, which is what storing the suite's answers would take -- see
        `_table_budget` for where the line falls and why.

        Measured on the delivered class files too, not only on the source, and that
        is the reading that matters on a JVM: a large array initializer in Java
        compiles to bytecode that fills the array element by element, so a class
        file can be far larger than the source that produced it, and a submission
        could equally put the table straight into the pool as a long string
        constant.  The class file is where the table actually is, whatever the
        source looked like.
        """
        budget = self._table_budget()
        offenders: list[str] = []
        total = 0
        for path in self.files:
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            # Documentation and the pinned RFCs are not data tables.
            if path.suffix.lower() in (".md", ".3", ".txt", ".html", ".pdf"):
                continue
            if path.name in ("ChangeLog", "FAQ", "INDEX", "README", "LICENSE"):
                continue
            if size > budget:
                offenders.append(f"{rel(self.repo, path)}: {size} bytes")
            if path.suffix in (".java", ".bin", ".dat", ".tbl", ".data"):
                total += size
        for config in self.installed_configs():
            handle = self.jar_handle(config)
            if handle is None:
                continue
            for entry in handle.entries():
                if entry.size > budget:
                    offenders.append(
                        f"[{config}] the jar entry {entry.name} inflates to "
                        f"{entry.size} bytes"
                    )
        if offenders:
            return (
                False,
                f"{len(offenders)} file(s) exceed the {budget}-byte per-file budget, "
                f"which is room enough to store answers rather than compute them",
                sorted(offenders)[:20],
            )
        return (
            True,
            f"{total} bytes of Java and data; every file and jar entry under the "
            f"{budget}-byte budget",
            [],
        )


    # == G5: the release contract is intact =================================
    #
    # The group the C-to-Rust form graded by compiling a consumer against both the
    # reference and the submission and comparing what the two reported.  That is
    # not available here, for a reason worth stating plainly: the reference
    # publishes `libz.so.1.3.1` and State B publishes `zlib-1.3.1.jar`, and nothing
    # can be compiled against both.  So these gates compare the submission against
    # the *contract's* pinned numbers instead, and the contract's own load-time
    # assertions are what keep those numbers honest.
    #
    # Two readings are taken wherever a number can be spelled twice, and the
    # duplication is the point.  `ZLIB_VERSION` exists as a string constant a
    # consumer's compiler inlines and as the value `Zlib.version()` returns at run
    # time; those are different numbers when a static initializer assigns something
    # other than the declared literal, and only the constant travels into an
    # already-compiled consumer.

    def _constant_values(self, config: str) -> dict[str, int | str] | None:
        """The ConstantValue attributes of the constants holder, by field name.

        Read from the class file rather than by reflection, and that is the
        substantive choice: reflection reports what the field holds after the static
        initializer ran, while the ConstantValue attribute is what javac copies into
        every consumer that names the constant.  A submission that declares
        `ZLIB_VERNUM = 4880` and then assigns something else in `<clinit>` disagrees
        with everything already compiled against it, and only this reading sees it.
        """
        holder = classfile.internal(self.expectations.constants_holder)
        for cls in self.jar_classes(config):
            if cls.this_class != holder:
                continue
            values: dict[str, int | str] = {}
            for field in cls.fields:
                value = field.constant_value(cls.pool)
                if value is not None:
                    values[field.name] = value
            return values
        return None

    def gate_version_unchanged(self) -> tuple[bool, str, list[str]]:
        """The library still reports 1.3.1, as a constant and at run time.

        Both halves are needed and they can disagree.  The constant is what a
        consumer's compiler inlines; `Zlib.version()` is what the consumer gets when
        it asks at run time, and zlib's own semantics make the pair load-bearing:
        `deflateInit` takes the caller's version string and returns
        `Z_VERSION_ERROR` when the major digits differ, so a submission that
        hardcodes one and edits the other breaks a check no round trip notices.

        The version is also the one fact where "same as the reference" would not be
        sufficient even if a reference were available: if the pinned tarball were
        ever moved, this gate should fail rather than follow it.  So the pinned
        constants in the contract are what it compares against.
        """
        pinned = self.contract["behavioral_contract"]["pinned_constants"]
        want_version = str(pinned["ZLIB_VERSION"])
        want_vernum = int(str(pinned["ZLIB_VERNUM"]), 16)
        configs = self.installed_configs()
        if not configs:
            return False, "no configuration installed; the version cannot be read", []
        problems: list[str] = []
        agreed: list[str] = []
        for config in configs:
            records = self._observer_records(config)
            reported = records.get("version")
            if reported is None:
                problems.append(
                    f"[{config}] the library would not report its version at run time"
                )
            elif reported.strip() != want_version:
                problems.append(
                    f"[{config}] Zlib.version() returns {reported.strip()!r}, "
                    f"the pinned release is {want_version!r}"
                )
            values = self._constant_values(config)
            if values is None:
                problems.append(
                    f"[{config}] {self.expectations.constants_holder} is not in the "
                    f"jar, so its constants cannot be read"
                )
                continue
            got_string = values.get("ZLIB_VERSION")
            if got_string != want_version:
                problems.append(
                    f"[{config}] the ZLIB_VERSION constant is {got_string!r}, "
                    f"expected {want_version!r}"
                )
            got_vernum = values.get("ZLIB_VERNUM")
            if got_vernum != want_vernum:
                problems.append(
                    f"[{config}] ZLIB_VERNUM is {got_vernum!r}, expected "
                    f"{want_vernum} (0x{want_vernum:x})"
                )
            for name, want in (
                ("ZLIB_VER_MAJOR", 1), ("ZLIB_VER_MINOR", 3),
                ("ZLIB_VER_REVISION", 1), ("ZLIB_VER_SUBREVISION", 0),
            ):
                if values.get(name) != want:
                    problems.append(
                        f"[{config}] {name} is {values.get(name)!r}, expected {want}"
                    )
            if not problems:
                agreed.append(f"[{config}] constant and run time both {want_version}")
        if problems:
            return False, "the reported version is not the pinned release", problems[:20]
        return (
            True,
            f"{want_version} (0x{want_vernum:x}) as a constant and at run time: "
            f"{'; '.join(agreed)}",
            [],
        )

    def gate_compile_flags_unchanged(self) -> tuple[bool, str, list[str]]:
        """compileFlags still describes the pinned type sizes.

        In C this function is how a binary consumer discovers the ABI it is talking
        to: the low bits give sizeof(uInt), sizeof(uLong), sizeof(voidpf) and
        sizeof(z_off_t) as 2-bit codes, and the upper bits record whether the build
        was debug, whether gzip support was compiled out, and whether the deflate
        fast path was forced.

        On a JVM none of those sizes vary, which is exactly why this is graded
        rather than dropped.  The value the port must report is the one the pinned C
        release reported on the graded platform -- 0xa9, meaning 4-byte uInt, 8-byte
        uLong, 8-byte pointer, 8-byte offset -- because a consumer that reads
        `compileFlags()` to decide how to talk to the library must get the same
        answer from both implementations or the migration changed the interface.
        Nothing in a round trip reads it, and a submission that returns 0 passes
        every behavioural case.

        The bit fields are decoded rather than compared as one number, so the report
        says which field is wrong instead of only that the number is.
        """
        want = int(
            str(self.contract["behavioral_contract"]["pinned_constants"]["zlibCompileFlags"]),
            16,
        )
        configs = self.installed_configs()
        if not configs:
            return False, "no configuration installed; the flags cannot be read", []
        problems: list[str] = []
        agreed: list[str] = []
        sizes = {0: 16, 1: 32, 2: 64, 3: "other"}
        fields = (
            ("sizeof(uInt)", 0), ("sizeof(uLong)", 2),
            ("sizeof(voidpf)", 4), ("sizeof(z_off_t)", 6),
        )
        for config in configs:
            records = self._observer_records(config)
            reported = records.get("compileflags")
            if reported is None:
                problems.append(
                    f"[{config}] the library would not report its compile flags"
                )
                continue
            try:
                got = int(reported.strip(), 16)
            except ValueError:
                problems.append(
                    f"[{config}] compileFlags() reported the unparseable "
                    f"{reported.strip()!r}"
                )
                continue
            if got == want:
                agreed.append(f"[{config}] 0x{got:x}")
                continue
            for label, shift in fields:
                mine = (got >> shift) & 0x3
                theirs = (want >> shift) & 0x3
                if mine != theirs:
                    problems.append(
                        f"[{config}] {label} reads as {sizes[mine]} bits, the pinned "
                        f"release reports {sizes[theirs]}"
                    )
            if (got >> 8) != (want >> 8):
                problems.append(
                    f"[{config}] the upper flag bits are 0x{got >> 8:x}, the pinned "
                    f"release reports 0x{want >> 8:x}"
                )
            if not any(f"[{config}]" in p for p in problems):
                problems.append(
                    f"[{config}] compileFlags() returns 0x{got:x}, the pinned value "
                    f"is 0x{want:x}"
                )
        if problems:
            return False, "the compile flags are not the pinned value", problems[:20]
        return (
            True,
            f"compileFlags() is 0x{want:x} with the pinned bit fields: "
            f"{'; '.join(agreed)}",
            [],
        )

    def gate_no_api_widening(self) -> tuple[bool, str, list[str]]:
        """No public class or member beyond the contract's closed world.

        The Java counterpart of the C form's exported-symbol gate, and it grades the
        same obligation: once a member is public a consumer can use it, and the next
        release cannot remove it without breaking that consumer, so an accidental
        export becomes a permanent commitment.  Java makes this easy to do by
        accident in a way C does not -- there is no version script to forget,
        `public` is a keystroke, and the fastest way to make a driver work is to
        widen something.

        Read from the class files' access flags rather than by reflection.  The
        structural suite asks the JVM the same question through `Surface.java`, and
        the duplication is deliberate: reflection reports the runtime view, and this
        reports the compile-time one, which is what `javac` shows a consumer.  A
        class file whose access flags disagree with what reflection reports is
        itself a finding no single reading would produce.

        Four exemptions, each for a reason that would otherwise fail correct work.
        Object's three overridable methods are allowed, since `toString` on a stream
        is a debugging courtesy and `equals`/`hashCode` travel with it.  Synthetic
        and bridge members are skipped because the compiler emits them and the
        submission did not write them.  A constructor of an abstract class is not
        counted, because `ZStream` declares none precisely so that `Deflater` and
        `Inflater` inherit it, and javac makes that inherited default public.

        And only classes in *exported* packages are graded, which is the exemption
        that matters most.  The contract permits an implementation package --
        `org.zlib.internal` -- and a class there has to be public for `org.zlib` to
        use it across the package boundary, while the module descriptor keeps it
        unreachable from outside.  Public-but-not-exported is not a published
        surface, so grading it would fail a submission for structuring its code the
        way the contract invites.  Which packages are exported is graded separately,
        by `module-descriptor-intact`.
        """
        if not self.expectations.api.get("closed_world", True):
            return True, "the contract does not close the world", []
        declared: set[str] = set()
        for name, spec in self.expectations.api_types.items():
            for method in spec.get("methods", []):
                descriptor = _descriptor_of(
                    method.get("params", []), method.get("returns", "void")
                )
                declared.add(f"{name}#{method['name']}{descriptor}")
            for field in spec.get("fields", []):
                declared.add(f"{name}#{field['name']}:{_type_descriptor(field['type'])}")
            for ctor in spec.get("constructors", []):
                declared.add(
                    f"{name}#<init>{_descriptor_of(ctor.get('params', []), 'void')}"
                )
            declared.add(f"{name}#toString()Ljava/lang/String;")
            declared.add(f"{name}#hashCode()I")
            declared.add(f"{name}#equals(Ljava/lang/Object;)Z")
        holder = self.expectations.constants_holder
        for name in self.expectations.int_constants:
            declared.add(f"{holder}#{name}:I")
        for name in self.expectations.string_constants:
            declared.add(f"{holder}#{name}:Ljava/lang/String;")
        abstract = {
            name for name, spec in self.expectations.api_types.items()
            if "abstract" in spec.get("modifiers", [])
            and spec.get("kind") != "interface"
        }
        exported = set(self.expectations.module_contract.get("exports", ()))
        offenders: list[str] = []
        counted = 0
        for config in self.installed_configs():
            for cls in self.jar_classes(config):
                dotted = cls.binary_name
                if not cls.is_public:
                    continue
                if cls.package not in exported:
                    continue
                if dotted not in self.expectations.api_types:
                    offenders.append(
                        f"[{config}] {dotted} is public in the exported package "
                        f"{cls.package} but the contract does not declare it"
                    )
                    continue
                for member in list(cls.fields) + list(cls.methods):
                    if not member.is_public or member.is_synthetic:
                        continue
                    if member.name == "<clinit>":
                        continue
                    if member.name == "<init>" and dotted in abstract:
                        continue
                    if member.descriptor.startswith("("):
                        key = f"{dotted}#{member.name}{member.descriptor}"
                    else:
                        key = f"{dotted}#{member.name}:{member.descriptor}"
                    counted += 1
                    if key not in declared:
                        offenders.append(
                            f"[{config}] {dotted}.{member.name} {member.descriptor} is "
                            f"public and undeclared"
                        )
        if not counted and not offenders:
            return False, "no installed jar to inspect", []
        if offenders:
            return (
                False,
                f"{len(offenders)} public element(s) beyond the contract's closed "
                f"world; a published surface is a promise to keep it",
                sorted(set(offenders))[:20],
            )
        return (
            True,
            f"{counted} public member(s), all declared by the contract "
            f"({self.expectations.member_count()} declared)",
            [],
        )

    def gate_module_descriptor_intact(self) -> tuple[bool, str, list[str]]:
        """The module descriptor is the pinned one, in the pinned place.

        This is the gate with no counterpart in the C form at all, and it is the
        closest Java equivalent of the version script: `module-info.class` is what
        decides which packages a consumer on the module path can see, and dropping
        it does not fail visibly -- the jar keeps working as an automatic module on
        the class path, with everything in it readable.  A submission that ships no
        descriptor has quietly opened its internals to every consumer, and the only
        symptom is that a strong encapsulation error never happens.

        Four readings.  The descriptor must be at the jar root, because a
        `META-INF/versions/N/module-info.class` is only seen by some JDKs and only
        one is graded.  The module name must be the pinned one, since it is what a
        consumer writes in its own `requires`.  The exported set must be exactly the
        contract's -- an extra export is the widening this gate exists to catch, and
        a missing one makes the library unusable on the module path.  And it must
        not be `open`, because an open module surrenders encapsulation wholesale to
        reflection, which is the widening spelled in one word.
        """
        want_name = self.expectations.module_name
        want_exports = set(self.expectations.module_contract.get("exports", ()))
        want_path = self.expectations.module_contract.get(
            "descriptor_path", "module-info.class"
        )
        offenders: list[str] = []
        checked: list[str] = []
        for config in self.installed_configs():
            handle = self.jar_handle(config)
            if handle is None:
                offenders.append(f"[{config}] the installed jar is unreadable")
                continue
            names = set(handle.names())
            if want_path not in names:
                offenders.append(
                    f"[{config}] {want_path} is not in the jar; the artifact is an "
                    f"automatic module with no encapsulation"
                )
                continue
            for name in sorted(names):
                if name.startswith("META-INF/versions/") and name.endswith(
                    "module-info.class"
                ):
                    offenders.append(
                        f"[{config}] a versioned descriptor {name} shadows the root one"
                    )
            try:
                descriptor = handle.module_descriptor()
            except classfile.ClassFileError as exc:
                offenders.append(f"[{config}] {want_path} will not parse: {exc}")
                continue
            if descriptor is None:
                offenders.append(f"[{config}] the jar declares no module")
                continue
            if descriptor.name != want_name:
                offenders.append(
                    f"[{config}] the module is named {descriptor.name!r}, the "
                    f"contract pins {want_name!r}"
                )
            exported = descriptor.exported_packages()
            extra = sorted(exported - want_exports)
            missing = sorted(want_exports - exported)
            if extra:
                offenders.append(f"[{config}] the module also exports {extra}")
            if missing:
                offenders.append(f"[{config}] the module does not export {missing}")
            if descriptor.is_open:
                offenders.append(
                    f"[{config}] the module is open, which surrenders encapsulation "
                    f"to reflection"
                )
            qualified = [e.package for e in descriptor.exports if e.is_qualified]
            if qualified:
                offenders.append(
                    f"[{config}] the module has qualified exports {sorted(qualified)}, "
                    f"which name consumers the release does not have"
                )
            checked.append(
                f"[{config}] {descriptor.name} exports {sorted(exported)}"
            )
        if offenders:
            return False, "the module descriptor is not the pinned one", offenders[:20]
        if not checked:
            return False, "no installed jar to inspect", []
        return True, "; ".join(checked), []

    def gate_api_shape_unchanged(self) -> tuple[bool, str, list[str]]:
        """Every declared class and member is present with its declared shape.

        The complement of the widening gate: that one grades what was added, this
        one grades what was dropped or altered.  Both matter, and for the migration
        the second matters more -- a submission that omits `deflateSetHeader`'s
        counterpart has not finished the port, and a submission that quietly turns a
        `long` return into an `int` has changed what every consumer computes without
        changing anything a round trip observes.

        Descriptors are compared rather than names.  A method's name is not its
        identity in Java any more than in C: `inflateInit2` and `inflateInit` differ
        by a parameter, the contract's `symbol_map` deliberately collapses the C
        `_z` and `_64` variants onto single Java signatures because `long` is 64 bits
        everywhere on a JVM, and a check that read only names would accept any of
        those collapses being un-collapsed.

        The static and final flags are read too, because they are part of what a
        consumer compiles against: a constant that is not `final` is not inlined,
        and a method that gains or loses `static` changes every call site.
        """
        offenders: list[str] = []
        present = 0
        for config in self.installed_configs():
            by_name = {cls.binary_name: cls for cls in self.jar_classes(config)}
            for name, spec in sorted(self.expectations.api_types.items()):
                cls = by_name.get(name)
                if cls is None:
                    offenders.append(f"[{config}] {name} is not in the jar")
                    continue
                kind = spec.get("kind", "class")
                if kind == "interface" and not cls.is_interface:
                    offenders.append(f"[{config}] {name} should be an interface")
                if kind != "interface" and cls.is_interface:
                    offenders.append(f"[{config}] {name} should be a class")
                modifiers = set(spec.get("modifiers", []))
                if "abstract" in modifiers and not cls.is_abstract:
                    offenders.append(f"[{config}] {name} should be abstract")
                if "final" in modifiers and not cls.is_final:
                    offenders.append(f"[{config}] {name} should be final")
                extends = spec.get("extends")
                if extends and cls.super_class and classfile.internal(extends) != cls.super_class:
                    offenders.append(
                        f"[{config}] {name} extends "
                        f"{cls.super_class.replace('/', '.')}, expected {extends}"
                    )
                methods = {(m.name, m.descriptor) for m in cls.methods}
                fields = {(f.name, f.descriptor) for f in cls.fields}
                for member in spec.get("methods", []):
                    descriptor = _descriptor_of(
                        member.get("params", []), member.get("returns", "void")
                    )
                    if (member["name"], descriptor) not in methods:
                        offenders.append(
                            f"[{config}] {name}.{member['name']}{descriptor} is "
                            f"missing or has a different signature"
                        )
                        continue
                    present += 1
                    for found in cls.methods:
                        if (found.name, found.descriptor) != (
                            member["name"], descriptor
                        ):
                            continue
                        if bool(member.get("static")) != found.is_static:
                            offenders.append(
                                f"[{config}] {name}.{member['name']} static is "
                                f"{found.is_static}, expected "
                                f"{bool(member.get('static'))}"
                            )
                for member in spec.get("fields", []):
                    descriptor = _type_descriptor(member["type"])
                    if (member["name"], descriptor) not in fields:
                        offenders.append(
                            f"[{config}] the field {name}.{member['name']} "
                            f"({descriptor}) is missing or has a different type"
                        )
                    else:
                        present += 1
                for member in spec.get("constructors", []):
                    descriptor = _descriptor_of(member.get("params", []), "void")
                    if ("<init>", descriptor) not in methods:
                        offenders.append(
                            f"[{config}] the constructor {name}{descriptor} is missing"
                        )
                    else:
                        present += 1
            values = self._constant_values(config)
            if values is None:
                offenders.append(
                    f"[{config}] the constants holder is not in the jar"
                )
            else:
                for cname, want_int in sorted(self.expectations.int_constants.items()):
                    if values.get(cname) != want_int:
                        offenders.append(
                            f"[{config}] the constant {cname} is {values.get(cname)!r}, "
                            f"expected {want_int}"
                        )
                    else:
                        present += 1
                for cname, want_str in sorted(
                    self.expectations.string_constants.items()
                ):
                    if values.get(cname) != want_str:
                        offenders.append(
                            f"[{config}] the constant {cname} is {values.get(cname)!r}, "
                            f"expected {want_str!r}"
                        )
                    else:
                        present += 1
        if not present and not offenders:
            return False, "no installed jar to inspect", []
        if offenders:
            return (
                False,
                f"{len(offenders)} declared element(s) missing or reshaped; the "
                f"published interface must survive the migration",
                sorted(set(offenders))[:25],
            )
        return True, f"{present} declared element(s) present with the declared shape", []

    def gate_license_preserved(self) -> tuple[bool, str, list[str]]:
        """LICENSE and the license notices survive the migration.

        Non-mandatory, because it is a licensing failure rather than a migration
        failure and the two deserve different consequences -- but reported, since
        stripping the zlib license from a derivative work is a real problem and the
        report is where a maintainer would look for it.

        The LICENSE file is compared byte for byte against the baseline: a rewritten
        license is not a preserved one.  The notice in zlib.h is checked separately
        because in this task zlib.h is no longer the interface -- it is preserved as
        the *specification* the Java surface answers to -- and its copyright notice
        is the one a reader of that specification sees.
        """
        problems: list[str] = []
        baseline_license = self.baseline / "LICENSE"
        submitted = self.repo / "LICENSE"
        if not submitted.is_file():
            problems.append("LICENSE is missing from the submission")
        elif baseline_license.is_file():
            if submitted.read_bytes() != baseline_license.read_bytes():
                problems.append("LICENSE differs from the pinned upstream text")
        header = self.repo / "zlib.h"
        if header.is_file():
            text = read_text(header)
            for needle in ("Jean-loup Gailly", "Mark Adler", "warranty"):
                if needle not in text:
                    problems.append(f"zlib.h no longer carries {needle!r}")
        if problems:
            return False, "the license notices were not preserved", problems[:10]
        return True, "LICENSE is byte-identical to upstream and zlib.h keeps its notice", []

    def gate_no_test_mutation(self) -> tuple[bool, str, list[str]]:
        """The pinned specification material was not edited.

        Non-mandatory, and narrower than its name suggests, because the verifier's
        own tests are not in the repository -- they live in the image, and the
        submission cannot reach them.  What it *can* reach is the material the
        contract preserves, and in this task that material is the specification
        rather than an interface: the three RFCs are the wire formats the port must
        produce, and zlib.h is the semantics each Java member answers to.  Editing
        those to match a changed implementation, rather than changing the
        implementation to match them, inverts the task -- and for the RFCs it would
        be editing the definition of the file format.

        zlib.h is compared with whitespace normalized: a submission may reflow it,
        and a reflowed header that still documents the same semantics is not a
        mutation.  The declarations themselves are graded in detail by the
        structural cases; this gate reports the coarse fact.
        """
        problems: list[str] = []
        checked: list[str] = []
        preserved = list(self.contract["preserved_paths"]["paths"])
        for name in sorted(preserved):
            if name in ("zlib.h", "CMakeLists.txt"):
                # zlib.h is compared below with whitespace normalized, and
                # CMakeLists.txt has to change: it is the build the submission
                # rewrites to drive javac.
                continue
            base = self.baseline / name
            mine = self.repo / name
            if not base.is_file():
                continue
            if not mine.is_file():
                problems.append(f"{name} was deleted; it is part of the release")
                continue
            if mine.read_bytes() != base.read_bytes():
                problems.append(f"{name} was edited")
            else:
                checked.append(name)
        header_base = self.baseline / "zlib.h"
        header_mine = self.repo / "zlib.h"
        if header_base.is_file() and header_mine.is_file():
            def squash(text: str) -> str:
                return re.sub(r"\s+", " ", text).strip()
            if squash(read_text(header_mine)) != squash(read_text(header_base)):
                problems.append("zlib.h differs from upstream beyond whitespace")
            else:
                checked.append("zlib.h")
        if problems:
            return False, "pinned specification material was edited", problems[:10]
        return True, f"unchanged: {', '.join(checked)}", []
