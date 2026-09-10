"""The release still builds, for every platform it shipped for.

ChartMuseum ships prebuilt binaries for eleven platform pairs, and that matrix is
the release. Preserving it is part of the task, so it is measured rather than
assumed, in two halves that fail for different reasons:

* the project's own build entry point still works -- a submission that made the
  sources compile but broke ``make build`` has shipped nothing;
* every platform in the Makefile's own ``TARGETS`` still cross-compiles.

The second half is not redundant. A port can compile for linux/amd64 and fail for
windows/amd64 on path separators or syscall availability, or for a 32-bit target on
integer width in a length check. Gin abstracted none of that away, so a plain
net/http rewrite can genuinely regress it, and finding out at release time is the
kind of debt this benchmark exists to refuse.

``TARGETS`` is read from the submission's own Makefile *and* compared against the
list State A declared, because a submission that quietly dropped a platform would
otherwise be graded against the shorter list it now ships.

``make build-cross`` is deliberately not used: it shells out to ``go get`` for gox
on first use, which needs a network. The platforms are compiled directly with
GOOS/GOARCH, which is what gox does anyway.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

import toolchain as tc

#: State A's TARGETS, verbatim.
STATE_A_TARGETS = [
    "darwin/amd64", "darwin/arm64", "linux/amd64", "linux/386", "linux/arm",
    "linux/arm64", "linux/mips64le", "linux/ppc64le", "linux/s390x",
    "windows/amd64", "linux/loong64",
]

#: What `make build` is supposed to leave behind. Six rather than eleven: the
#: Makefile's own `build` target covers the platforms it builds by default, and the
#: other five are reached through the cross-compile loop below.
MAKE_ARTEFACTS = [
    "bin/linux/amd64/chartmuseum", "bin/darwin/amd64/chartmuseum",
    "bin/darwin/arm64/chartmuseum", "bin/windows/amd64/chartmuseum",
    "bin/linux/mips64/chartmuseum", "bin/linux/loongarch64/chartmuseum",
]

rep = tc.Report()


def makefile_targets(text: str) -> list[str]:
    m = re.search(r"^TARGETS\s*:?=\s*(.+)$", text, re.M)
    return m.group(1).split() if m else []


def main() -> int:
    source = tc.SOURCE
    if not source.is_dir():
        rep.record("source-published", False,
                   "the build module published no source tree, so the release "
                   "cannot be attempted")
        return rep.finish()

    makefile = source / "Makefile"
    if not rep.record("makefile-survives", makefile.is_file(),
                      "the Makefile is gone; it is the release entry point and "
                      "the project's own way of building itself", weight=2.0):
        return rep.finish()

    targets = makefile_targets(makefile.read_text())
    missing = [t for t in STATE_A_TARGETS if t not in targets]
    rep.record("targets-declared", not missing,
               "the Makefile no longer declares every release platform; missing "
               f"from TARGETS: {', '.join(missing)}" if missing else "",
               weight=2.0)

    env = tc.go_env()
    proc = tc.run(["make", "build"], cwd=source, env=env, timeout=3600,
                  log="make-build.log")
    if not rep.record("make-build", proc.returncode == 0,
                      "`make build` failed, so the release cannot be produced",
                      detail=tc.output_of(proc), weight=3.0):
        return rep.finish()

    absent = [p for p in MAKE_ARTEFACTS if not (source / p).is_file()]
    rep.record("make-build-artefacts", not absent,
               "`make build` returned 0 but produced no artifact at: "
               + ", ".join(absent) if absent else "", weight=2.0)

    # Every declared platform, compiled the way gox would. One check each, so a
    # submission that regressed one platform loses one platform.
    with tempfile.TemporaryDirectory(prefix="srb-cross-") as tmp:
        for target in STATE_A_TARGETS:
            goos, _, goarch = target.partition("/")
            out = Path(tmp) / f"chartmuseum-{goos}-{goarch}"
            cenv = tc.go_env(GOOS=goos, GOARCH=goarch, CGO_ENABLED="0")
            proc = tc.run(
                [tc.GO, "build", "-o", str(out), "-ldflags",
                 "-w -X main.Version=0.15.0 -X main.Revision=grading",
                 "./cmd/chartmuseum"],
                cwd=source, env=cenv, timeout=1800,
                log=f"cross-{goos}-{goarch}.log")
            rep.record(f"cross-{goos}-{goarch}",
                       proc.returncode == 0 and out.is_file(),
                       f"{target} no longer cross-compiles",
                       detail=tc.output_of(proc), weight=1.0)

    # The release scripts read `make get-version` to name every artifact.
    proc = tc.run(["make", "get-version"], cwd=source, env=env, timeout=300,
                  log="get-version.log")
    reported = (proc.stdout or "").strip().splitlines()[-1].strip() \
        if proc.stdout else ""
    rep.record("get-version", reported == "0.15.0",
               f"`make get-version` reports {reported!r}, not '0.15.0'; the "
               f"release artifacts would be named wrongly",
               detail=tc.output_of(proc), weight=1.0)

    # And the native binary the Makefile just built has to run. A cross-compile
    # only proves it links.
    built = source / "bin/linux/amd64/chartmuseum"
    if built.is_file():
        proc = tc.run([str(built), "--version"], timeout=120,
                      log="release-version.log")
        combined = tc.output_of(proc)
        rep.record("release-binary-runs", "0.15.0" in combined,
                   "the binary `make build` produced does not report version "
                   "0.15.0", detail=combined, weight=1.0)

    return rep.finish({"targets": targets, "platforms": len(STATE_A_TARGETS)})


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        rep.record("release-module", False,
                   f"the release module raised {type(exc).__name__}: {exc}")
        rep.finish()
        raise
