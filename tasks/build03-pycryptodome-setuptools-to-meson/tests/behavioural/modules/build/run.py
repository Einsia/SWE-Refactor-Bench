"""Build the matrix once, publish a ledger, and score whether it built.

Runs first and required. Five configurations, each in its own copied tree, each
through the same front end:

    python -m build --wheel --no-isolation

The eleven modules after this one read the trees this leaves behind. They never
build anything themselves, which is why a slow module here buys a fast suite and
why two modules can never disagree about what the submission produced.

This module is scored rather than treated as setup. "The default configuration
builds a wheel that installs" is a measurement, and when it fails the right place
for that news is the report, with the log path attached -- not a harness error
that reads as the verifier having broken.

State A is *not* built here, and that is not an omission. Its side of every
comparison comes from `data/`, recorded once from a real setuptools build, because
a ground truth a run can recompute is not a ground truth: expectations regenerated
per run agree with whatever the run produced. The five configurations below are the
submission's, compared against those recordings.

What keeps the recordings honest is that this image *can* build State A -- both
backends are installed and the Dockerfile asserts at image-build time that the
front end reaches each of them. So "the image is broken" and "the submission is
broken" are distinguishable: the reference migration builds cleanly in the same
image, on the same toolchain, against the same recorded numbers.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ.get("SRB_SUITE_DIR", "/tests/behavioural") + "/lib")

import builder  # noqa: E402
import selftest_runner  # noqa: E402

REPO = Path(os.environ["SRB_REPO"])
WORK = Path(os.environ["SRB_SUITE_WORK"])
RESULT = Path(os.environ["SRB_RESULT"])
MODULE_ID = os.environ.get("SRB_MODULE_ID", "build")

CHECKS: list[dict] = []


def record(name: str, ok: bool, detail: str, *, weight: float = 1.0, required: bool = False):
    """Append one check in the shape the runner reads.

    The key is `verdict`, not `status`, and `summary`, not `message`. Both matter:
    `Check.from_dict` treats a missing or unrecognised verdict as `error` rather
    than as a pass, so a module that names the field wrongly scores zero on every
    check it wrote -- and stage 2 pays nothing unless every weighted module passes
    every scored check, so that zero would take the whole stage down with it
    however well the submission built. `status` at
    the top level of the file means something different (the module errored or
    timed out), which is why it is not reused here.

    The id is *not* prefixed with the module name: the runner namespaces every
    check by its module when it collects, so prefixing here produces `build/build/...`.
    """
    CHECKS.append({
        "id": name,
        "verdict": "pass" if ok else "fail",
        "weight": weight,
        "required": required,
        "summary": detail,
    })
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)


def finish(notes: str = ""):
    """Always write a result, even on the way out of an exception.

    A stage that produces no result file is scored as a harness failure, which
    hides whatever the submission actually did. So this is called from a finally.

    `notes` is plural because that is the key the runner lifts into the report;
    a singular `note` is read by nothing and would take the traceback with it.
    No top-level `status` is written on purpose -- the runner reserves `error`
    and `timeout` there for "the module did not run", and a build that failed is
    a module that ran and measured a failure.
    """
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps({
        "schema": "swerefactor.module-result/1",
        "module": MODULE_ID,
        "checks": CHECKS,
        "notes": notes,
    }, indent=2), encoding="utf-8")
    print(f"wrote {len(CHECKS)} checks to {RESULT}", flush=True)


def manifest(root: Path) -> dict:
    """sha256 of every regular file under root, keyed by relative path.

    Taken before anything is built, so "did the build write into the source tree"
    has an answer that is not a guess.
    """
    out = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith((".git/", "__pycache__/")) or "/__pycache__/" in rel:
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)

    # ---- the pristine snapshot, before any build touches anything ---------- #
    pristine = manifest(REPO)
    (WORK / "pristine.json").write_text(
        json.dumps({"root": str(REPO), "files": pristine}, sort_keys=True), encoding="utf-8")
    record("the_delivered_tree_was_snapshotted", len(pristine) > 100,
           f"{len(pristine)} files recorded before any configuration was built",
           weight=0.5, required=True)

    # ---- the five configurations ------------------------------------------ #
    root = WORK / "builds"
    records = []
    for config in builder.MATRIX:
        started = time.time()
        try:
            rec = builder.build_one(REPO, config, root, timeout=3000)
        except Exception as exc:  # a shim that will not compile, a tree that will not copy
            rec = builder.Build(name=config.name, ok=False, rc=None,
                                note=f"the harness could not set this configuration up: {exc}")
        records.append(rec)
        required = config.name == "default"
        weight = 2.0 if required else 1.0
        detail = (f"{config.about}: built in {rec.seconds:.0f}s"
                  if rec.ok else
                  f"{config.about}: {rec.note or 'failed'} (rc={rec.rc}, log: {rec.log})")
        record(f"configuration_builds[{config.name}]", rec.ok, detail,
               weight=weight, required=required)
        print(f"  -- {config.name} finished in {time.time() - started:.0f}s", flush=True)

    builder.Ledger.publish(root, records).write()

    # ---- the self-test, once, against the default install ----------------- #
    default = next((r for r in records if r.name == "default" and r.ok), None)
    if default is None:
        record("the_self_test_suite_runs", False,
               "no default install to run pycryptodome's own suite against", weight=2.0)
    else:
        payload = selftest_runner.run(Path(default.install), WORK / "selftest-default.json",
                                      timeout=5400)
        counts = payload.get("counts", {})
        ok = not payload.get("crashed") and payload.get("recorded", 0) > 0
        record("the_self_test_suite_runs", ok,
               (f"{payload.get('collected', 0)} instances over {payload.get('recorded', 0)} ids "
                f"in {payload.get('seconds', 0)}s: {counts}") if ok else
               f"the suite did not run: rc={payload.get('child_rc')} "
               f"{str(payload.get('output', ''))[-600:]}", weight=2.0)

    return 0


if __name__ == "__main__":
    notes = ""
    try:
        main()
    except Exception as exc:  # noqa: BLE001 -- the result file matters more
        import traceback
        notes = traceback.format_exc()[-2000:]
        record("the_build_module_ran_to_completion", False,
               f"the build module raised: {exc}", weight=1.0, required=True)
    finally:
        finish(notes)
