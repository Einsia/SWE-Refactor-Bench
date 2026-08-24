#!/usr/bin/env python3
"""The two drivers must accept the same op vocabulary.

Not a grading path, and it consults no submission.  It answers the one question an
identity run against State A structurally cannot, and the reason it exists is that
the gap it closes cost 23% of this stage's checks to every submission alike.

`harness.runners.sandbox()` has two implementations behind one name.  A tree that
still presents one CommonJS package is routed to `js/oracle.cjs`; a ported one goes
to `js/sandbox.mjs`.  State A is pre-migration by definition, so grading State A
against itself -- the control that returns 40.000/40, and the only control this
stage had -- exercises `oracle.cjs` and never once reaches `sandbox.mjs`.  Every
real submission is ported, so every real submission is graded by the driver that
control cannot see.

What that hid: `sandbox.mjs` passed every `renderer.set()` value through a
primitive-only assertion, while `oracle.cjs` fed the same value through
`devirtDeep`, which recurses into arrays.  So a list -- which is what
`renderer.set('paths', [...])` takes, and what `modules/builtins` emits for every
one of its probes -- was answered by one face and rejected by the other, before the
submission was called at all.  Measured: 238 checks, the identical ids lost by two
independently ported trees.  Stage 2 pays for a submission that answered every
scored check or it pays nothing, so the size of that loss decides nothing and its
existence decides everything: a *flawless* port is a port that fails 238 checks, it
is paid zero for the stage, it cannot enter stage 3, and the whole 100 points are
unwinnable by anyone.  A grading run could not show it, because a rejected op is
reported as a behavioural failure.

So the assertion here is parity, not a list of blessed shapes: each op is put
through both faces and the two must *agree on acceptance*.  Only acceptance -- the
values cannot be compared, because one side is real Stylus and the other is a
fixture.  Comparing values is the graded run's job, and it already does it; what it
cannot do is notice that one face never got the chance to answer.

A fifteen-line fixture core stands in for a submission, which is what lets this run
at image-build time rather than waiting for a submission to reveal the problem.  It
also re-checks the invariant the primitive rule was protecting, because the fix must
not buy coverage with an escape: what crosses must belong to the realm, not to the
host.

Run it against a built stage-2 image, where State A is baked in:

    docker run --rm swerefactor/pf02-behavioural:4 \\
        python3 /tests/behavioural/selftest.py

Exit status is the verdict: 0 when every assertion holds.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
JS = HERE / "lib" / "js"

VPROJ = "/proj"
VRUNTIME = "/runtime"

# A core that records what it was handed instead of compiling anything.  The driver
# needs a default export callable as `stylus(src, options)`; the rest is the surface
# `buildRenderer` touches.  `render()` returns a report rather than CSS -- nothing
# here is compared against the oracle's output, only against what crossed.
FIXTURE_CORE = """
export default function stylus(src, options) {
  const settings = {};
  return {
    options,
    set(k, v) { settings[k] = v; return this; },
    include(p) { settings['include:' + p] = true; return this; },
    define(n, v) { settings['define:' + n] = v; return this; },
    render() {
      const paths = settings.paths;
      return JSON.stringify({
        isArray: Array.isArray(paths),
        // `Array.prototype` here is the realm's, so this is true only if the value
        // was rebuilt inside the realm rather than handed across.
        protoIsRealmArray: paths ? Object.getPrototypeOf(paths) === Array.prototype : null,
        values: paths ? Array.prototype.slice.call(paths) : null,
        filename: settings.filename,
        optionKeys: options ? Object.keys(options).sort() : null,
        // A host array would reach the *host* Function through its constructor,
        // which is the escape the primitive-only rule existed to prevent.
        reachesRealmFunction: (function () {
          if (!paths) return null;
          try { return paths.constructor.constructor === Function; } catch (e) { return 'threw'; }
        })(),
      });
    },
    renderSync() { return this.render(); },
  };
}
"""

# The shapes the suite emits.  `paths` as a list is the one that was rejected; the
# others are here so that a future tightening cannot quietly drop them either.
#
# Deliberately import-free and stat-free: real Stylus answers these on the oracle
# side, so an op that needed a file to exist would fail there for a reason that has
# nothing to do with the vocabulary, and parity would report a false violation.
JOB_OPS = [
    {"id": "paths-list", "kind": "render", "source": "a\n  color red\n",
     "set": {"filename": f"{VPROJ}/test/cases/probe.styl",
             "paths": [f"{VPROJ}/test/cases", VRUNTIME]}},
    {"id": "paths-empty", "kind": "render", "source": "a\n  color red\n",
     "set": {"paths": []}},
    {"id": "primitives-still-cross", "kind": "render", "source": "a\n  color red\n",
     "set": {"filename": f"{VPROJ}/test/cases/probe.styl", "compress": True,
             "include css": False}},
    {"id": "structured-option", "kind": "render", "source": "a\n  color red\n",
     "options": {"imports": []},
     "set": {"paths": [f"{VPROJ}/test/cases"], "compress": False}},
]


def _run(driver: str, job: dict, out: Path, tag: str) -> dict:
    job_path = out / f"job-{tag}.json"
    res_path = out / f"result-{tag}.json"
    job_path.write_text(json.dumps(job))
    argv = ["node"]
    if driver.endswith(".mjs"):
        argv.append("--experimental-vm-modules")
    argv += [str(JS / driver), str(job_path), str(res_path)]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    if not res_path.is_file():
        raise SystemExit(
            f"{driver} wrote no result (rc={proc.returncode}); this is a harness "
            f"fault, not a verdict\nstderr:\n{proc.stderr[-2000:]}"
        )
    return json.loads(res_path.read_text())


def _by_id(data: dict) -> dict:
    return {r["id"]: r for r in data.get("results", [])}


def _err(r: dict) -> str:
    return str((r.get("error") or {}).get("message", ""))[:220]


def main() -> int:
    ap = argparse.ArgumentParser(description="sandbox/oracle vocabulary parity")
    ap.add_argument("--state-a", type=Path,
                    default=Path(os.environ.get("PF03_STATE_A", "/opt/state-a")),
                    help="the immutable Stylus 0.63.0 tree the oracle loads")
    ap.add_argument("--report", type=Path, help="write the findings as JSON")
    args = ap.parse_args()

    # Required, not skipped.  A parity test that silently degrades to a one-sided
    # test when State A is absent would have reported success on the very bug it
    # exists to catch.
    if not (args.state_a / "index.js").is_file():
        raise SystemExit(
            f"no State A at {args.state_a}: this test compares two drivers and "
            "cannot run one-sided. Run it inside a built stage-2 image, or pass "
            "--state-a."
        )

    failures: list[str] = []
    findings: dict[str, object] = {}

    with tempfile.TemporaryDirectory(prefix="pf02-selftest-") as td:
        tmp = Path(td)
        core = tmp / "fixture" / "src" / "core"
        core.mkdir(parents=True)
        (core / "index.js").write_text(FIXTURE_CORE)

        sb = _run("sandbox.mjs", {
            "coreRoot": str(core),
            "platform": {"sync": False, "cwd": VPROJ, "runtimeRoot": VRUNTIME},
            "vfs": {},
            "ops": JOB_OPS,
        }, tmp, "sandbox")

        orc = _run("oracle.cjs", {
            "stateARoot": str(args.state_a),
            "mounts": {VPROJ: str(args.state_a), VRUNTIME: str(args.state_a)},
            "ops": JOB_OPS,
        }, tmp, "oracle")

    if sb.get("loadError"):
        failures.append(f"sandbox could not load the fixture core: {sb['loadError']}")

    sbr, orr = _by_id(sb), _by_id(orc)
    findings["sandbox"] = {k: bool(v.get("ok")) for k, v in sbr.items()}
    findings["oracle"] = {k: bool(v.get("ok")) for k, v in orr.items()}

    # ---- the parity assertion ------------------------------------------------
    for op in JOB_OPS:
        oid = op["id"]
        s, o = sbr.get(oid), orr.get(oid)
        if s is None or o is None:
            missing = "sandbox" if s is None else "oracle"
            failures.append(f"{oid}: {missing} returned no result for this op")
            continue
        if bool(s.get("ok")) == bool(o.get("ok")):
            continue
        if o.get("ok"):
            failures.append(
                f"{oid}: the oracle answered this op and the sandbox refused it -- "
                f"{_err(s)}. The faces disagree about the vocabulary, so every "
                f"submission graded through the sandbox loses these checks for a "
                f"reason no submission can affect."
            )
        else:
            failures.append(
                f"{oid}: the sandbox answered this op and the oracle refused it -- "
                f"{_err(o)}. An expectation cannot be produced, so the check is "
                f"unanswerable rather than merely lost."
            )

    # ---- what actually crossed, on the sandbox side --------------------------
    got = sbr.get("paths-list", {})
    if got.get("ok"):
        seen = json.loads(got["value"]["css"])
        findings["paths-list"] = seen
        want = [f"{VPROJ}/test/cases", VRUNTIME]
        if not seen["isArray"]:
            failures.append("paths-list: the value did not arrive as an array")
        if seen["values"] != want:
            failures.append(f"paths-list: contents changed in transit: {seen['values']}")
        if seen["filename"] != f"{VPROJ}/test/cases/probe.styl":
            failures.append("paths-list: the primitive beside it did not cross")
        # Paths stay virtual on this face: the realm's platform speaks virtual
        # paths, so unlike the oracle there is nothing to devirtualise here.
        if any(p.startswith(str(HERE)) or "/opt/state-a" in p for p in seen["values"]):
            failures.append(f"paths-list: a host path crossed: {seen['values']}")
        # The invariant the primitive-only rule was protecting.
        if seen["protoIsRealmArray"] is not True:
            failures.append(
                "paths-list: the array is not an in-realm object, so a host object "
                "crossed -- this is the escape the primitive rule guarded and the "
                "fix must not reintroduce it"
            )
        if seen["reachesRealmFunction"] is not True:
            failures.append(
                "paths-list: constructor.constructor did not resolve to the realm's "
                "own Function; the realm's prototype chain is not intact"
            )

    empty = sbr.get("paths-empty", {})
    if empty.get("ok"):
        seen = json.loads(empty["value"]["css"])
        if seen["values"] != []:
            failures.append(f"paths-empty: expected [], got {seen['values']}")

    opt = sbr.get("structured-option", {})
    if opt.get("ok"):
        seen = json.loads(opt["value"]["css"])
        # `inRealmOptions` rebuilds the options object and attaches the platform;
        # both have to be there or the core is being handed something else.
        if "imports" not in (seen["optionKeys"] or []):
            failures.append(
                f"structured-option: options.imports did not survive: "
                f"{seen['optionKeys']}"
            )
        if "platform" not in (seen["optionKeys"] or []):
            failures.append(
                "structured-option: the realm's platform is not on the options "
                "object, so the core has no capability to reach the filesystem"
            )

    report = {"ok": not failures, "failures": failures, "findings": findings}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True))

    for line in failures:
        print(f"FAIL  {line}", file=sys.stderr)
    if failures:
        print(f"\n{len(failures)} assertion(s) failed", file=sys.stderr)
        return 1
    print(f"ok  both faces accept all {len(JOB_OPS)} op shapes this suite emits, "
          "and every value that crossed into the realm belongs to it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
