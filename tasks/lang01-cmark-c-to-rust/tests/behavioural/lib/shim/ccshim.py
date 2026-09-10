#!/usr/bin/env python3
"""C/C++ compiler shim installed ahead of the real compilers during grading.

`cc`, `gcc`, `c++`, `g++` and friends resolve to this program, which classifies
every invocation it sees -- compile, link, CMake probe, or query -- and appends
the verdict to the JSONL ledger at $CCSHIM_LOG.  The ledger is the evidence the
`compiler-shim-clean` gate reads: whether a repository C source was compiled
during the graded build is a line in a file rather than an inference from a build
log.

The distinctions it draws matter for fairness in both directions.

* A Rust `cdylib`/`staticlib` is linked by `rustc` invoking a C compiler as the
  *linker driver*.  Refusing that would make the task impossible, so link-only
  invocations are forwarded to the real compiler untouched.
* CMake identifies its compilers by compiling a probe into its own scratch
  directory.  That probe is not repository code, so it is allowed; a submission
  is free to keep C enabled as a CMake language for linking purposes.
* Compiling a source file that belongs to the submission is the action the
  migration is supposed to have eliminated, and it is always recorded as such.

$CCSHIM_MODE decides what *happens* to that last case, and it is the only thing
the mode changes:

    record   (the graded default) forward it to the real compiler and record it.
    enforce  refuse it, loudly, with exit 97.

`record` is what stage 2 runs, because stage 2 grades behaviour against
expectations recorded from State A and State A is the C implementation: a mode
that stops the build is a mode in which the 4,122 behavioural cases have no
library to ask, so the C library scores zero on its own corpus.  What compiled is
still in the ledger, `compiler-shim-clean` still fails on it, and stage 1 -- where
`no-c-sources`, `no-c-in-build` and `rust-is-primary` are required gates and a
failure is a zero for the whole submission -- is where it is charged.  Refusing
the compile would charge it here as well: a module that did not pass every scored
check closes stage 2, whatever its weight.

`enforce` is kept for the case the ledger cannot cover.  A submission that
reaches a compiler by a name this shim does not shadow leaves nothing in the
ledger to read, and running the build again under `enforce` answers "does this
tree still need a C compiler" directly rather than by absence of evidence.  It is
a diagnostic, not the graded path, and a run that takes it says so in its notes.

Unset means `record`, matching the graded path; an unrecognised value is an error
rather than a guess, because the two modes differ in whether a build succeeds.
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
LINK_INPUT_SUFFIXES = (".o", ".obj", ".a", ".so", ".rlib", ".lo", ".dylib")

# CMake identifies and sanity-checks its compilers by compiling small generated
# probes.  Those are CMake's own files, not repository code, so refusing them
# would only force a cosmetic CMake restructuring without proving anything.
#
# The exemption is deliberately narrow, because "it's just a probe" is otherwise
# an obvious way to smuggle a real compile through.  A source is exempt only if
# all four hold:
#
#   1. it lives outside the repository under test;
#   2. it carries one of CMake's own generated basenames;
#   3. it is no larger than a real probe (CMake's compiler-id source is the big
#      one at ~25 KB; nothing CMake generates approaches the cap); and
#   4. it does not mention cmark.
#
# (4) is the check that actually holds the line.  Copying `blocks.c` to
# /tmp/conftest.c defeats (1) and (2) and may fit under (3), but any source that
# forms part of this library must either name a `cmark_*` symbol or include
# `cmark.h`; one that does neither cannot define an export or be called from one.
# A probe's object file is also recorded, so the link-input cross-check can
# confirm none of them ended up inside a shipped artifact.
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
    }
)
PROBE_MAX_BYTES = 65536
PROBE_FORBIDDEN_TOKENS = (b"cmark", b"CMARK")


MODE_RECORD = "record"
MODE_ENFORCE = "enforce"
MODES = (MODE_RECORD, MODE_ENFORCE)


def mode() -> str:
    """How a repository compile is handled.  See the module docstring."""
    value = (os.environ.get("CCSHIM_MODE") or MODE_RECORD).strip().lower()
    if value not in MODES:
        sys.stderr.write(
            f"ccshim: CCSHIM_MODE={value!r} is not one of {', '.join(MODES)}.\n"
            "ccshim: refusing to guess -- the modes differ in whether a build "
            "that compiles C succeeds.\n"
        )
        raise SystemExit(99)
    return value


def log_event(event: dict) -> None:
    path = os.environ.get("CCSHIM_LOG")
    if not path:
        return
    event["ts"] = round(time.time(), 3)
    event["pid"] = os.getpid()
    event["ppid"] = os.getppid()
    # Recorded on every line: a reader looking at a forwarded compile has to be
    # able to tell "the shim was configured to permit this" from "the shim was
    # bypassed", and those two produce the same event otherwise.
    event.setdefault("mode", mode())
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.flush()
    except OSError:
        pass


def is_cmake_probe(source: str) -> bool:
    """True only for CMake's own generated probes, outside the repo, cmark-free."""
    path = os.path.abspath(source)
    repo = os.environ.get("CCSHIM_REPO")
    if repo:
        repo = os.path.abspath(repo)
        if path == repo or path.startswith(repo + os.sep):
            return False
    if os.path.basename(path) not in PROBE_BASENAMES:
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

    if verdict == "compile" and mode() == MODE_ENFORCE:
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
