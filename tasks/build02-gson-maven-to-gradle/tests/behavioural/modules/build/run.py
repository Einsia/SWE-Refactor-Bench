#!/usr/bin/env python3
"""Run the build matrix once, and publish it for the nine modules that read it.

This module is first and ``required``, and it does almost no judging.  Its job is
to produce the thing everything downstream measures: eight invocations of the
agent's project, each against its own pristine copy of the delivered sources,
offline, with every build engine the tree does not declare shadowed by a failing
stub.

Why it is a module and not image setup
-------------------------------------
Because "it builds" is a measurement, not preparation.  A submission whose
assemble configuration does not reach four jars has not migrated the build system,
and that has to appear in the report as a failed check with its log attached --
not as a harness error that leaves the stage with no result.

Why it runs once
----------------
The `full` configuration alone runs 1328 test cases, and the `tests`, `entries`,
`derive`, `runtime`, `manifest`, `version`, `bytecode`, `publication` and `jpms`
modules are separate processes.  Building in each would cost seventy-two
invocations instead of eight and would let two modules disagree about what the
build did.  The trees stay under ``$SRB_SUITE_WORK`` and this module writes
``builds.json`` beside them: which configuration a directory is, what argv
produced it, what it returned, and the path to its full log.

What it does *not* do
---------------------
Score the matrix configuration by configuration.  A build that failed is charged
by the checks that wanted its artefacts, each naming what it could not read;
charging it again here would make one broken build look like two.  The four checks
below are the ones nothing else can make: the sources were prepared and
snapshotted, the engines this tree does not declare are genuinely off PATH, the
assemble configuration produced the four jars, and nothing shelled out to one of
those engines while doing it.

Which build system it runs
--------------------------
Whichever one the delivered tree declares -- see ``lib/dialect.py``.  Every check
in this stage reads artefacts, and State A is where each of those expectations was
recorded from, so a stage that could only drive Gradle could not build its own
oracle and could not show its expectations are satisfiable.  Whether the migration
happened is stage 1's question, asked over the files, with required gates; this
stage asks whether the build *works*, and asks it of both trees in the same terms.
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
import make_stubs  # noqa: E402
import matrix  # noqa: E402

SUITE = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"))
SHARED = Path(os.environ.get("SRB_SUITE_WORK", "/tmp/srb-work"))
WORK = Path(os.environ.get("SRB_WORK", "/tmp/srb-build"))
RESULT = Path(os.environ["SRB_RESULT"])
REPO = Path(os.environ.get("SRB_REPO", "/workspace/repo"))

checks: list[dict] = []


def record(cid: str, ok: bool, summary: str = "", detail: str = "",
           weight: float = 1.0, required: bool = False) -> bool:
    entry = {"id": cid, "verdict": "pass" if ok else "fail", "weight": weight}
    if summary:
        entry["summary"] = summary
    if detail:
        entry["detail"] = detail[-4000:]
    if required:
        entry["required"] = True
    checks.append(entry)
    return ok


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    SHARED.mkdir(parents=True, exist_ok=True)

    # ---- the delivered sources, snapshotted before any build touched them ----
    # --baseline is what makes the cleanup exact rather than heuristic: no file
    # that shipped in State A is ever deleted, which is how
    # metrics/src/main/resources/ParseBenchmarkData.zip survives a sweep that
    # removes stray archives.  Maven leftovers are deliberately NOT cleaned:
    # their presence is measured, not tidied away.
    report = SHARED / "prepare_report.json"
    prep = subprocess.run(
        [sys.executable, str(SUITE / "lib" / "prepare_workspace.py"),
         "--repo", str(REPO), "--report", str(report),
         "--baseline", str(SUITE / "data" / "sources.json")],
        capture_output=True, text=True, timeout=1800, check=False)
    (WORK / "prepare.log").write_text((prep.stdout or "") + (prep.stderr or ""))
    print((prep.stdout or "").strip()[-2000:])
    prepared = prep.returncode == 0 and Path(builder.DELIVERED).is_dir()
    record("sources-prepared", prepared,
           "" if prepared else
           "the delivered tree could not be snapshotted, so no build in this "
           "stage would be running on known sources",
           detail="" if prepared else (prep.stdout or "") + (prep.stderr or ""),
           weight=1.0, required=True)
    if not prepared:
        return finish()

    # ---- every build engine the tree does not declare, shadowed by a stub ----
    # The stubs catch a vendored copy and record the argv and cwd of whatever
    # reached for one.  The engine the delivered tree declares is exempt: it is the
    # program this stage invokes, so stubbing it would measure nothing.  Which one
    # that is comes from the delivered build files, never from what a build calls.
    engine = builder.delivered_dialect()
    keep = tuple(engine.keep_on_path)
    try:
        make_stubs.build(builder.STUBS, keep=keep)
        stub_err = ""
    except OSError as exc:
        stub_err = "%r" % (exc,)
    installed = sorted(p.name for p in Path(builder.STUBS).iterdir()
                       if p.is_file() and not p.name.endswith(".calls")) \
        if Path(builder.STUBS).is_dir() else []
    want = set(make_stubs.installed(keep))
    shadowed = not stub_err and want <= set(installed) and \
        not (set(keep) & set(installed))
    record("engines-shadowed", shadowed,
           "" if shadowed else
           "the build-engine stubs were not installed, so a build that reaches "
           "for an engine this tree does not declare could have found a real one",
           detail="" if shadowed else
           "dialect: %s\nkept on PATH: %s\nmissing stubs: %s\ninstalled: %s\n%s"
           % (engine.name, ", ".join(keep) or "nothing",
              ", ".join(sorted(want - set(installed))) or "none",
              ", ".join(installed) or "nothing", stub_err),
           weight=1.0, required=True)
    if not shadowed:
        return finish()

    # ---- the matrix ---------------------------------------------------------
    builds = []
    started = time.time()
    for build in matrix.builds():
        t0 = time.time()
        build.execute()
        builds.append(build)
        print("%-11s %-10s %5.0fs  %d/%d jars" % (
            build.name, "ok" if build.ok else "FAILED", time.time() - t0,
            len(build.present_jars()), len(builder.PROJECTS)))

    ledger = builder.persist(builds, str(SHARED / "buildstate"))
    (SHARED / "builds.json").write_text(json.dumps(ledger, indent=1))
    builder.dump_summary(builds, str(WORK / "summary.json"))

    by_name = {b.name: b for b in builds}
    assemble = by_name["assemble"]
    record("assemble-produces-four-jars", assemble.complete,
           "" if assemble.complete else
           "the `assemble` configuration did not produce a jar for all four "
           "projects; every parity module downstream measures those jars",
           detail="" if assemble.complete else assemble.failure_summary(),
           weight=4.0, required=True)

    # ---- did anything reach for an engine the tree does not declare? ---------
    # A Gradle submission that shells out to `mvn` for the steps it could not port
    # is caught here, and so is one whose build forks `gradlew` and would have
    # needed a network.  The exemption is the declared engine only, so this is a
    # scored check for either dialect rather than one State A fails by
    # construction.  Whether Maven is really *gone* from the tree is still stage
    # 1's `maven_retired`, over the files, with a reviewer.
    calls = make_stubs.any_stub_called(builder.STUBS)
    record("no-forbidden-engine-invoked", not calls,
           "" if not calls else
           "the delivered build invoked %s, which this tree does not declare as "
           "its build system" % ", ".join(sorted({c[0] for c in calls})),
           detail="" if not calls else
           "\n".join("%s  cwd=%s  argv=%s" % (c[0], c[2], c[3])
                     for c in calls[:40]),
           weight=1.0)

    ok = sum(1 for b in builds if b.ok)
    print("matrix: %d/%d configurations built in %.0fs"
          % (ok, len(builds), time.time() - started))
    return finish()


def finish() -> int:
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps({
        "checks": checks,
        "metadata": {
            "ledger": str(SHARED / "builds.json"),
            "build_root": builder.WORK,
            "delivered": builder.DELIVERED,
            "stubs": builder.STUBS,
            "dialect": builder.delivered_dialect().name,
            "configurations": list(matrix.ORDER),
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
               "the build module raised %s: %s" % (type(exc).__name__, exc),
               required=True)
        finish()
        raise
