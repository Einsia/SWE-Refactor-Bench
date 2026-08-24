#!/usr/bin/env python3
"""Install failing stubs for the Autotools toolchain, plus the probe-defeating
compiler wrappers used by the feature-detection audit checks.

Two independent mechanisms:

* `--dir D` writes stubs for autoconf/automake/libtool/... into D, which the
  harness puts first on PATH. If State B still reaches for the old toolchain,
  it fails here even if a future base image happens to ship those binaries.
  Each invocation is appended to D/.calls so the audit suite can report
  *which* tool was called and by what.

* `--reject-flags` writes a `cc`/`gcc` wrapper that pretends not to understand
  the given compiler flags (exits non-zero when any appears in argv). Pointing
  CMAKE_C_COMPILER at it makes `check_c_compiler_flag(-mavx512f …)` fail, so a
  build that *probes* drops the corresponding macro while a build that
  *hardcodes* it does not. Same idea for `--hide-headers`, which makes the
  wrapper fail any compile that includes one of the named headers.
"""
import argparse
import os
import stat

FORBIDDEN = [
    "autoconf", "autoreconf", "automake", "aclocal", "autoheader",
    "autom4te", "autoscan", "autoupdate", "ifnames",
    "libtool", "libtoolize", "glibtoolize", "glibtool",
]

STUB = """#!/bin/sh
# SWERefactorBench: {name} is not available in State B.
printf '%s\\n' "$(date +%s) {name} $*" >> "{calls}" 2>/dev/null || true
echo "{name}: command not found (SWERefactorBench: State B must not require GNU Autotools)" >&2
exit 127
"""

WRAPPER = r"""#!/bin/sh
# SWERefactorBench compiler wrapper: emulates a compiler that lacks certain
# capabilities, so that genuine feature probes observe the absence.
REAL={real}
LOG={log}
printf '%s\n' "$*" >> "$LOG" 2>/dev/null || true

for arg in "$@"; do
  case "$arg" in
{flag_cases}
  esac
done

{header_check}

exec "$REAL" "$@"
"""

FLAG_CASE = """    {flag})
      echo "{basename}: error: unrecognized command-line option '{flag}'" >&2
      exit 1
      ;;"""

HEADER_CHECK = r"""
# Fail any compilation whose preprocessed form pulls in a hidden header.
for arg in "$@"; do
  case "$arg" in
    *.c)
      if [ -f "$arg" ]; then
        for hdr in {headers}; do
          if grep -q "include[[:space:]]*[<\"]$hdr[>\"]" "$arg" 2>/dev/null; then
            echo "$0: fatal error: $hdr: No such file or directory" >&2
            exit 1
          fi
        done
      fi
      ;;
  esac
done
"""


def write_exec(path: str, text: str) -> None:
    with open(path, "w") as fh:
        fh.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir")
    ap.add_argument("--wrapper-dir")
    ap.add_argument("--reject-flags", default="")
    ap.add_argument("--hide-headers", default="")
    ap.add_argument("--real-cc", default="/usr/bin/gcc")
    args = ap.parse_args()

    if args.dir:
        os.makedirs(args.dir, exist_ok=True)
        calls = os.path.join(args.dir, ".calls")
        open(calls, "a").close()
        for name in FORBIDDEN:
            write_exec(os.path.join(args.dir, name),
                       STUB.format(name=name, calls=calls))
        print("stubs: %d Autotools binaries shadowed in %s" % (len(FORBIDDEN), args.dir))

    if args.wrapper_dir:
        d = args.wrapper_dir
        os.makedirs(d, exist_ok=True)
        log = os.path.join(d, ".cc-calls")
        open(log, "a").close()
        flags = [f for f in args.reject_flags.split(",") if f]
        headers = [h for h in args.hide_headers.split(",") if h]
        cases = "\n".join(
            FLAG_CASE.format(flag=f, basename="cc") for f in flags
        ) or "    --srb-never-matches) ;;"
        hdr = HEADER_CHECK.format(headers=" ".join(headers)) if headers else ""
        body = WRAPPER.format(real=args.real_cc, log=log,
                              flag_cases=cases, header_check=hdr)
        for name in ("cc", "gcc", "srb-cc"):
            write_exec(os.path.join(d, name), body)
        print("wrapper: %s rejects flags=%s hides=%s"
              % (d, flags or "-", headers or "-"))


if __name__ == "__main__":
    main()
