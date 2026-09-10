#!/usr/bin/env python3
"""Failing stubs for every build engine State B must not need.

The stage-2 image installs none of them and asserts their absence at build time,
but absence alone is a weak instrument.  A submission can vendor an `mvn` script,
check in a `maven-wrapper.jar` and a launcher, or have a Gradle task fork one
through `exec`.  A stub that is *present, first on PATH and always fails* turns
"we hope nothing calls Maven" into evidence:

  * it exits 127, so a build that depends on it fails loudly rather than silently
    skipping the step that would have produced the difference;
  * it appends its argv and its cwd to `<dir>/<name>.calls`, so the report can
    name what tried to run it.

The engine the delivered tree declares is exempt -- passed as `keep` -- because it
is the tool this suite invokes, and stubbing it would mean measuring nothing.  For
a Gradle submission that is `gradle`, and `mvn` is stubbed; for the Maven tree the
migration starts from it is the other way round.  Which one it is comes from
`dialect.detect()`, i.e. from the delivered build files, so a submission cannot
widen its own exemption by *calling* something.

The wrappers are stubbed either way.  `gradlew` and `mvnw` would try to fetch a
distribution over a network that does not exist, and a build that needs one has
made this suite's own invocation not the build.

`any_stub_called()` reads the records back.  One line in any of them means the
delivered build still delegates to a toolchain it does not declare.
"""
import os
import stat

STUBS = (
    "mvn", "mvnw", "mvnw.cmd", "mvnd", "maven",
    "gradle", "gradlew", "gradlew.bat",
    "ant", "ant.sh", "antRun",
    "ivy",
    "sbt", "bazel", "buck", "scons", "leiningen", "lein",
    "make", "gmake", "cmake", "ninja",
)

TEMPLATE = """#!/bin/sh
# SWERefactorBench verifier stub for `%(name)s`.
#
# State B must not need this program. Every invocation is recorded and fails.
{
  printf '%%s\\t%%s\\t' "$(date -u +%%FT%%TZ)" "$(pwd)"
  for a in "$@"; do printf '%%s ' "$a"; done
  printf '\\n'
} >> "%(log)s" 2>/dev/null || true
echo "%(name)s: not available in the verifier (SWERefactorBench build02)" >&2
exit 127
"""


def installed(keep=()):
    """The stubs that will be created, given the engines to leave alone."""
    return tuple(n for n in STUBS if n not in set(keep))


def build(stub_dir, keep=()):
    """Create the stub directory and return its path.

    `keep` names the engines the delivered tree declares; no stub is written for
    them, so the suite's own invocation reaches the real program.  Any stub left
    over from an earlier call is removed, so a kept engine cannot be shadowed by a
    file this function wrote before it knew the dialect.
    """
    os.makedirs(stub_dir, exist_ok=True)
    for name in set(keep):
        try:
            os.remove(os.path.join(stub_dir, name))
        except OSError:
            pass
    for name in installed(keep):
        path = os.path.join(stub_dir, name)
        log = os.path.join(stub_dir, "%s.calls" % name)
        with open(path, "w") as fh:
            fh.write(TEMPLATE % {"name": name, "log": log})
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP
                 | stat.S_IXOTH)
    return stub_dir


def any_stub_called(stub_dir):
    """[(program, timestamp, cwd, argv)] for every recorded invocation."""
    hits = []
    if not os.path.isdir(stub_dir):
        return hits
    for fn in sorted(os.listdir(stub_dir)):
        if not fn.endswith(".calls"):
            continue
        prog = fn[:-len(".calls")]
        try:
            with open(os.path.join(stub_dir, fn), errors="replace") as fh:
                for line in fh:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    parts = line.split("\t", 2)
                    while len(parts) < 3:
                        parts.append("")
                    hits.append((prog, parts[0], parts[1], parts[2]))
        except OSError:
            pass
    return hits


def clear(stub_dir):
    """Forget recorded calls, so a later phase can attribute its own."""
    if not os.path.isdir(stub_dir):
        return
    for fn in os.listdir(stub_dir):
        if fn.endswith(".calls"):
            try:
                os.remove(os.path.join(stub_dir, fn))
            except OSError:
                pass


if __name__ == "__main__":
    import sys
    print(build(sys.argv[1] if len(sys.argv) > 1 else "/tmp/srb-stubs"))
