#!/usr/bin/env python3
"""Run the build matrix once, and publish it for every other module that reads it.

This module is first, and it does almost no judging.  Its job is
to produce the thing everything downstream measures: seven full configurations of
the delivered build system, configured, built and installed out of tree from a
pristine copy of the delivered sources, plus four configure-only probes driven by
a compiler that pretends to lack a capability -- eleven in all.

Why it is a module and not image setup
--------------------------------------
Because "it configures, builds and installs" is a measurement, not preparation.
A submission whose default configuration does not build has not migrated the
build system, and that has to appear in the report as a failed check with its log
attached -- not as a harness error that leaves the stage with no result.

Why it runs once
----------------
The matrix costs minutes.  The `configure`, `install`, `symbols`, `isa`,
`macros`, `hardening`, `ctest`, `corpus`, `package`, `headers`, `pkgconfig`,
`cmake-package`, `probe`, `evidence` and `provenance` modules are
separate processes; building in each would cost eleven builds per module instead
of eleven in total.  The trees stay under ``$SRB_SUITE_WORK`` and this module writes
``builds.json`` beside them -- which configuration a directory is, what argv
produced it, what each step returned, and the path to its full output.

What it does *not* do
---------------------
Score the matrix configuration by configuration.  That is the `configure`
module's subject, and duplicating it here would charge a broken build twice.
The three checks below are the ones nothing else can make: the sources were
prepared, the old toolchain is genuinely off PATH, and the default configuration
reached an install tree.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ.get("SRB_SUITE_DIR", "/tests/behavioural") + "/lib")

import builder  # noqa: E402

SUITE = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"))
SHARED = Path(os.environ.get("SRB_SUITE_WORK", "/tmp/srb-work"))
WORK = Path(os.environ.get("SRB_WORK", "/tmp/srb-build"))
RESULT = Path(os.environ["SRB_RESULT"])
REPO = Path(os.environ.get("SRB_REPO", "/workspace/repo"))

#: name -> Build kwargs.  Seven full configurations; the option combinations a
#: task-specific module wants beyond these it can build for itself.
MATRIX = {
    "ninja-default": dict(generator="Ninja"),
    "make-default": dict(generator="Unix Makefiles"),
    "ninja-minimal": dict(generator="Ninja", options={"SODIUM_MINIMAL": "ON"}),
    "ninja-static-only": dict(generator="Ninja",
                              options={"SODIUM_BUILD_SHARED": "OFF",
                                       "SODIUM_BUILD_STATIC": "ON"}),
    "ninja-shared-only": dict(generator="Ninja",
                              options={"SODIUM_BUILD_SHARED": "ON",
                                       "SODIUM_BUILD_STATIC": "OFF"}),
    "ninja-notests": dict(generator="Ninja",
                          options={"SODIUM_BUILD_TESTS": "OFF"}),
    "ninja-debug": dict(generator="Ninja", build_type="Debug"),
}

#: Configure-only builds whose compiler lacks something.  A build system that
#: *probes* for the capability drops the corresponding macro; one that hardcodes
#: it does not, which is what the `probe` module reads out of these trees.
PROBES = {
    "probe-nosysrandom": dict(hide_headers="sys/random.h"),
    "probe-noavx512": dict(reject_flags="-mavx512f"),
    "probe-noaes": dict(reject_flags="-maes,-mpclmul"),
    # A compiler that does not understand -fstack-protector, which State A's
    # AX_CHECK_COMPILE_FLAG probe would discover and skip.  This is the
    # behavioural form of "the hardening flags are probed for": a build system
    # that probes configures fine here and leaves the flag off the compile line;
    # one that writes the flag into CMAKE_C_FLAGS cannot get past CMake's own
    # compiler check, because that check compiles with CMAKE_C_FLAGS and this
    # wrapper exits 1 on sight of the flag.  No recorded ground truth is needed --
    # the reaction is forced either way.
    "probe-nohardening": dict(reject_flags="-fstack-protector"),
}

checks: list[dict] = []


def record(cid: str, ok: bool, summary: str = "", detail: str = "",
           weight: float = 1.0) -> bool:
    entry = {"id": cid, "verdict": "pass" if ok else "fail", "weight": weight}
    if summary:
        entry["summary"] = summary
    if detail:
        entry["detail"] = detail[-4000:]
    checks.append(entry)
    return ok


def make_probe(name: str, reject_flags: str = "", hide_headers: str = ""):
    """A configure-only Build whose CC is a capability-lacking wrapper."""
    wdir = SHARED / "wrappers" / name
    argv = [sys.executable, str(SUITE / "lib" / "make_stubs.py"),
            "--wrapper-dir", str(wdir), "--real-cc", "/usr/bin/gcc"]
    if reject_flags:
        argv.append("--reject-flags=" + reject_flags)
    if hide_headers:
        argv.append("--hide-headers=" + hide_headers)
    subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                   timeout=120, check=False)
    return builder.Build(name, generator="Ninja", compiler=str(wdir / "cc"),
                         configure_only=True)


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    SHARED.mkdir(parents=True, exist_ok=True)

    # ---- the delivered sources, snapshotted before any build touched them ----
    report = SHARED / "prepare_report.json"
    prep = subprocess.run(
        [sys.executable, str(SUITE / "lib" / "prepare_workspace.py"),
         "--repo", str(REPO), "--report", str(report),
         "--snapshot", builder.DELIVERED],
        capture_output=True, text=True, timeout=1800, check=False)
    (WORK / "prepare.log").write_text((prep.stdout or "") + (prep.stderr or ""))
    prepared = prep.returncode == 0 and Path(builder.DELIVERED).is_dir()
    record("sources-prepared", prepared,
           "" if prepared else
           "the delivered tree could not be snapshotted, so no build in this "
           "stage would be running on known sources",
           detail="" if prepared else (prep.stdout or "") + (prep.stderr or ""),
           weight=1.0)
    if not prepared:
        return finish()

    # ---- which build system is being measured, decided once -----------------
    #
    # Written to the shared marker before anything reads it, so every module in
    # the suite answers the same way even after a build tree is pruned.  What it
    # selects is which driver the measurements run through, not what they expect:
    # the install tree, the symbol table and the 119 compile lines are compared
    # against the same recorded State A either way.
    flavour = builder.flavour_of(builder.DELIVERED)
    builder.record_flavour(flavour)
    boot = builder.bootstrap_snapshot(builder.DELIVERED)
    if boot is not None:
        (WORK / "bootstrap.log").write_text(boot.out)
        record("snapshot-bootstrapped", boot.ok,
               "" if boot.ok else
               "the delivered tree has no `configure` and could not generate one, "
               "so no configuration in the matrix has a build system to run",
               detail="" if boot.ok else boot.tail(40), weight=0.0)
    print("flavour: %s%s" % (flavour, "" if boot is None else
                             " (bootstrapped rc=%d)" % boot.rc))

    # ---- the retired toolchain, shadowed by stubs that record their callers --
    stubs = subprocess.run(
        [sys.executable, str(SUITE / "lib" / "make_stubs.py"),
         "--dir", builder.STUBS],
        capture_output=True, text=True, timeout=300, check=False)
    calls = Path(builder.STUBS) / ".calls"
    shadowed = stubs.returncode == 0 and calls.is_file()
    record("autotools-shadowed", shadowed,
           "" if shadowed else
           "the Autotools stubs were not installed, so a build that reaches for "
           "autoconf could have found the real one",
           detail="" if shadowed else (stubs.stdout or "") + (stubs.stderr or ""),
           weight=1.0)
    if not shadowed:
        return finish()

    # ---- the matrix ----------------------------------------------------------
    builds: dict[str, builder.Build] = {}
    started = time.time()
    for name, kwargs in MATRIX.items():
        t0 = time.time()
        build = builder.Build(name, **kwargs)
        build.execute()
        builds[name] = build
        print("%-20s %-10s %5.0fs  %s" % (
            name, "installed" if build.installed else "incomplete",
            time.time() - t0, build.failure_summary().splitlines()[0]))

    for name, kwargs in PROBES.items():
        t0 = time.time()
        build = make_probe(name, **kwargs)
        build.execute()
        builds[name] = build
        print("%-20s %-10s %5.0fs" % (
            name, "configured" if build.configured else "unconfigured",
            time.time() - t0))

    ledger = builder.persist(builds, str(SHARED / "buildstate"))
    (SHARED / "builds.json").write_text(json.dumps(ledger, indent=1))

    default = builds["ninja-default"]
    record("default-configuration-installs", default.installed,
           "" if default.installed else
           "the default configuration (Ninja, Release) did not reach an install "
           "tree; every behavioural module downstream measures that tree",
           detail="" if default.installed else default.failure_summary(),
           weight=4.0)

    reached = sum(1 for b in builds.values()
                  if (b.configured if b.configure_only else b.installed))
    print("matrix: %d/%d configurations reached their terminal step in %.0fs"
          % (reached, len(builds), time.time() - started))
    return finish()


def finish() -> int:
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps({
        "checks": checks,
        "metadata": {
            "ledger": str(SHARED / "builds.json"),
            "build_root": builder.BUILD_ROOT,
            "delivered": builder.DELIVERED,
            "stubs": builder.STUBS,
            "configurations": sorted(MATRIX) + sorted(PROBES),
            "flavour": builder.delivered_flavour(),
        },
    }, indent=1))
    failed = [c["id"] for c in checks if c["verdict"] != "pass"]
    print("%d/%d checks passed%s" % (
        len(checks) - len(failed), len(checks),
        ("; failed: " + ", ".join(failed)) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                       # a result must always exist
        record("build-module", False,
               "the build module raised %s: %s" % (type(exc).__name__, exc))
        finish()
        raise
