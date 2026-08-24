#!/usr/bin/env python3
"""C/C++ compiler shim installed ahead of the real compilers during grading.

The migration claim under test is that no C source from this repository is
compiled any more.  Rather than inferring that from the build log, the verifier
makes it structurally impossible: `cc`, `gcc`, `c++`, `g++` and friends resolve
to this shim, which refuses to compile repository sources and records every
invocation it saw.

The distinction it draws matters for fairness in both directions.

* CMake identifies its compilers, and checks their ABI, by compiling and linking
  a probe into its own scratch directory.  That probe is not repository code, so
  it is allowed.  This is what lets a submission keep `project(zlib C)` -- a
  CMakeLists that still declares C is not itself a migration failure, and a
  submission should not have to restructure its project() call to be gradeable.
* Link-only invocations are forwarded untouched.  A javac/jar build has no link
  step of its own, so nothing in a correct submission needs this; it is here
  because CMake's probes do link, and because refusing a link would report a
  toolchain shape rather than a compile.
* Anything that compiles a source file belonging to the submission is refused,
  loudly, with a non-zero exit.  That is the action the migration is supposed to
  have eliminated.

Every decision is appended to the JSONL ledger at $CCSHIM_LOG, which the
audit gates read afterwards.  The ledger is the evidence; this program only
enforces and records.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

COMPILE_FLAGS = {"-c", "-S", "-E", "--compile"}
SOURCE_SUFFIXES = (
    ".c", ".cc", ".cpp", ".cxx", ".c++", ".C", ".CC", ".CPP", ".CXX",
    ".m", ".mm", ".M", ".s", ".S", ".sx", ".i", ".ii", ".re", ".inc",
)
LINK_INPUT_SUFFIXES = (".o", ".obj", ".a", ".so", ".lo", ".dylib")

# CMake identifies and sanity-checks its compilers by compiling small generated
# probes, and zlib's own configure adds more: check_include_file for sys/types.h,
# stdint.h, stddef.h and unistd.h, check_type_size for off64_t, and
# check_function_exists for fseeko.  Those are CMake's files, not repository code,
# so refusing them would only force a cosmetic CMake restructuring without
# proving anything -- and would make the configure impossible to pass at all.
#
# The exemption is deliberately narrow, because "it's just a probe" is otherwise
# an obvious way to smuggle a real compile through.  A source is exempt only if
# all four hold:
#
#   1. it lives outside the repository under test;
#   2. it carries one of CMake's own generated basenames, or sits in one of
#      CMake's own scratch directories;
#   3. it is no larger than a real probe (CMake's compiler-id source is the big
#      one at ~25 KB; nothing CMake generates approaches the cap); and
#   4. it names nothing from zlib.
#
# (4) is the check that actually holds the line.  Copying `deflate.c` to
# /tmp/conftest.c defeats (1) and (2) and may fit under (3), but every one of the
# eighteen translation units State A shipped names `zlib.h`, and any source that
# could define one of its exports or be called from one must name at least one of
# the tokens below -- the export macro, one of the public types, or one of the
# entry points themselves.  A probe's object file is also recorded, so the
# link-input cross-check can confirm none of them ended up in a shipped artifact.
PROBE_BASENAMES = frozenset(
    {
        "CMakeCCompilerId.c",
        "CMakeCXXCompilerId.c",
        "CMakeCXXCompilerId.cpp",
        "CMakeCCompilerABI.c",
        "CMakeCXXCompilerABI.cpp",
        "testCCompiler.c",
        "testCXXCompiler.cxx",
        "feature_tests.c",
        "feature_tests.cxx",
        "src.c",
        "src.cxx",
        "conftest.c",
        "CheckIncludeFile.c",
        "CheckIncludeFiles.c",
        "CheckFunctionExists.c",
        "CheckSymbolExists.c",
        "CheckTypeSize.c",
    }
)

# The basename list cannot be complete, and the gap is not hypothetical:
# check_type_size() names its generated source after the result variable, so
# zlib's `check_type_size(off64_t OFF64_T)` compiles a file called `OFF64_T.c`.
# Refusing it does not catch any cheating -- it makes the check report "off64_t
# unknown", which silently drops -D_LARGEFILE64_SOURCE=1 from the graded build
# and makes the submission's CMakeCache disagree with the reference's over
# something the submission never controlled.
#
# So directories are recognised as well as basenames.  These are the scratch
# trees CMake generates into, always under a `CMakeFiles` component of a build
# directory: CMakeTmp and CMakeScratch/TryCompile-* for try_compile, CheckTypeSize
# for the size probes, and <version>/CompilerId* for compiler identification.
# Anything a project generates for itself lands elsewhere, so this widens the
# exemption to CMake's own scratch without reaching build outputs.
PROBE_SCRATCH_DIRS = frozenset({"CMakeTmp", "CMakeScratch", "CheckTypeSize"})
PROBE_SCRATCH_PREFIXES = ("CompilerId", "TryCompile-")
PROBE_MAX_BYTES = 65536
PROBE_FORBIDDEN_TOKENS = (
    b"zlib", b"ZLIB", b"zconf", b"zutil",
    b"z_stream", b"ZEXPORT", b"ZEXTERN", b"Bytef", b"uLong", b"uInt",
    b"deflate", b"inflate", b"gzopen", b"gzread", b"gzwrite",
    b"adler32", b"crc32", b"compress2", b"uncompress",
)


def log_event(event: dict) -> None:
    path = os.environ.get("CCSHIM_LOG")
    if not path:
        return
    event["ts"] = round(time.time(), 3)
    event["pid"] = os.getpid()
    event["ppid"] = os.getppid()
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.flush()
    except OSError:
        pass


def in_cmake_scratch(path: str) -> bool:
    """True when a path lies inside one of CMake's own generated scratch trees.

    Anchored on a `CMakeFiles` component so a directory an agent happens to name
    `CMakeTmp` somewhere else does not qualify, and only components *after* that
    anchor are considered.
    """
    parts = path.split(os.sep)
    if "CMakeFiles" not in parts:
        return False
    for part in parts[parts.index("CMakeFiles") + 1 :]:
        if part in PROBE_SCRATCH_DIRS or part.startswith(PROBE_SCRATCH_PREFIXES):
            return True
    return False


def is_cmake_probe(source: str) -> bool:
    """True only for CMake's own generated probes, outside the repo, free of anything from the repository."""
    path = os.path.abspath(source)
    repo = os.environ.get("CCSHIM_REPO")
    if repo:
        repo = os.path.abspath(repo)
        if path == repo or path.startswith(repo + os.sep):
            return False
    if os.path.basename(path) not in PROBE_BASENAMES and not in_cmake_scratch(path):
        return False
    try:
        if os.path.getsize(path) > PROBE_MAX_BYTES:
            return False
        with open(path, "rb") as handle:
            body = handle.read(PROBE_MAX_BYTES)
    except OSError:
        return False
    return not any(token in body for token in PROBE_FORBIDDEN_TOKENS)


def classify(argv: list[str]) -> tuple[str, list[str], list[str]]:
    """Return (verdict, source_inputs, link_inputs).

    Verdicts: "compile" (a translation unit is being compiled), "link" (only
    already-compiled inputs), "probe" (compiling CMake's own scratch source),
    or "other" (a query such as --version or -dumpmachine).
    """
    sources: list[str] = []
    link_inputs: list[str] = []
    wants_compile = False
    skip_next = False
    takes_value = {
        "-o", "-I", "-L", "-D", "-U", "-include", "-isystem", "-iquote",
        "-idirafter", "-MF", "-MT", "-MQ", "-x", "-Xlinker", "-Xpreprocessor",
        "-Xassembler", "-B", "-T", "-u", "--param", "-aux-info",
    }
    for index, arg in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if arg in takes_value:
            skip_next = True
            continue
        if arg in COMPILE_FLAGS:
            wants_compile = True
            continue
        if arg.startswith("-"):
            continue
        if index == 0:
            continue
        if arg.endswith(SOURCE_SUFFIXES):
            sources.append(arg)
        elif arg.endswith(LINK_INPUT_SUFFIXES):
            link_inputs.append(arg)

    if sources:
        if all(is_cmake_probe(s) for s in sources):
            return "probe", sources, link_inputs
        return "compile", sources, link_inputs
    if wants_compile:
        # -c with no recognizable source: treat as a compile attempt rather than
        # letting an unrecognized spelling through.
        return "compile", sources, link_inputs
    if link_inputs:
        return "link", sources, link_inputs
    return "other", sources, link_inputs


def output_of(argv: list[str]) -> str | None:
    """The `-o` target, recorded so probe objects can be traced to link inputs."""
    for index, arg in enumerate(argv):
        if arg == "-o" and index + 1 < len(argv):
            return os.path.abspath(argv[index + 1])
        if arg.startswith("-o") and len(arg) > 2 and not arg.startswith("-of"):
            return os.path.abspath(arg[2:])
    return None


def real_compiler(name: str) -> str | None:
    """Locate the genuine compiler, skipping this shim's own directory."""
    shim_dir = os.path.dirname(os.path.abspath(__file__))
    override = os.environ.get("CCSHIM_REAL_" + name.upper().replace("+", "P"))
    if override and os.path.exists(override):
        return override
    for directory in os.environ.get("CCSHIM_PATH", "").split(os.pathsep):
        if not directory or os.path.abspath(directory) == shim_dir:
            continue
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def main() -> int:
    invoked = os.path.basename(sys.argv[0])
    argv = sys.argv[1:]
    verdict, sources, link_inputs = classify([invoked] + argv)

    if verdict == "compile":
        log_event(
            {
                "event": "reject",
                "tool": invoked,
                "verdict": verdict,
                "sources": sources[:20],
                "argv": argv[:60],
                "cwd": os.getcwd(),
            }
        )
        sys.stderr.write(
            f"ccshim: refusing to compile C/C++ source with '{invoked}'.\n"
            f"ccshim: inputs: {' '.join(sources[:8]) or '(implicit)'}\n"
            "ccshim: this repository is expected to contain no C/C++ code to\n"
            "ccshim: compile. Linking is permitted; compiling is not.\n"
        )
        return 97

    target = real_compiler(invoked)
    if target is None:
        log_event(
            {"event": "missing-real", "tool": invoked, "verdict": verdict}
        )
        sys.stderr.write(f"ccshim: no real '{invoked}' available\n")
        return 98

    log_event(
        {
            "event": "forward",
            "tool": invoked,
            "verdict": verdict,
            "sources": sources[:20],
            "link_inputs": [os.path.abspath(p) for p in link_inputs[:40]],
            "output": output_of(argv),
            "argv": argv[:60],
            "cwd": os.getcwd(),
            "real": target,
        }
    )
    completed = subprocess.run([target] + argv)
    log_event(
        {
            "event": "forwarded-result",
            "tool": invoked,
            "verdict": verdict,
            "returncode": completed.returncode,
        }
    )
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
