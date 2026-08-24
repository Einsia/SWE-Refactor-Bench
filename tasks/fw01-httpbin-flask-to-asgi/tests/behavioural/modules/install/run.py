"""Rebuild the submission from source, offline, into a venv the rest share.

This module exists first and is ``required``, for two reasons.

*It is a measurement.*  "Installs from its own source, offline" is part of the
migration rather than setup that happens before scoring starts: a tree that does
not install cannot be measured for behaviour by anything after it.

The wheelhouse this installs from is the COMPLETE one, and that is deliberate.
State A declares Flask, and State A is the oracle every scored expectation in this
stage was recorded from, so a wheelhouse that cannot install it could never show
this suite is satisfiable -- and because this module is ``required``, a missing
Flask wheel scored State A zero for the whole stage rather than costing it this
module.  Whether the tree still needs a distribution the agent was never given is
recorded at the end as an unscored note, because that is stage 1's question and
``flask_retired`` owns it there, with a gate that zeroes the submission.

*It is what every other module runs against.*  The venv goes in
``$SRB_SUITE_WORK/venv``, is built once, and is read by the ten pytest modules
after it.  Doing it per module would mean eleven installs; doing it in the image
would mean grading an environment the submission did not produce.

Nothing the agent left in the tree is trusted.  A stale ``.venv``, an
``*.egg-info``, a smuggled wheel or a ``__pycache__`` could each make a broken
``pyproject.toml`` import successfully, so they are removed from a *copy* of the
tree -- the graded tree itself is left exactly as submitted, because stage 3
compares against it and the report has to describe what was handed in.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(os.environ.get("SRB_REPO", "/workspace/repo"))
WORK = Path(os.environ.get("SRB_WORK", "/tmp/srb-install"))
SHARED = Path(os.environ["SRB_SUITE_WORK"])
RESULT = Path(os.environ["SRB_RESULT"])
WHEELHOUSE = os.environ.get("SRB_WHEELHOUSE", "/opt/wheelhouse")
PINS = os.environ.get("SRB_PINS", "/opt/pins/pinned-target.txt")

#: The agent's own wheelhouse, reproduced. Nothing is installed from it; the
#: unscored observation at the end of this module is its only reader.
WHEELHOUSE_PRUNED = os.environ.get("SRB_WHEELHOUSE_PRUNED",
                                   "/opt/wheelhouse-pruned")

#: Names that make a submitted tree lie about whether it can be installed.
ARTEFACTS = ("__pycache__", "*.egg-info", ".pytest_cache", ".venv", "venv",
             ".tox", ".mypy_cache", "node_modules", "build", "dist")

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


def scrub(tree: Path) -> list[str]:
    removed = []
    for pattern in ARTEFACTS:
        for path in sorted(tree.rglob(pattern)):
            removed.append(str(path.relative_to(tree)))
            shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
    for suffix in ("*.pyc", "*.whl"):
        for path in sorted(tree.rglob(suffix)):
            removed.append(str(path.relative_to(tree)))
            path.unlink(missing_ok=True)
    return removed


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    SHARED.mkdir(parents=True, exist_ok=True)

    if not REPO.is_dir():
        record("submission-exists", False,
               f"nothing to install: {REPO} does not exist", required=True)
        return finish()
    record("submission-exists", True, weight=0.0)

    source = SHARED / "source"
    if source.exists():
        shutil.rmtree(source)
    shutil.copytree(REPO, source, symlinks=True,
                    ignore=shutil.ignore_patterns(".git"))
    removed = scrub(source)
    (WORK / "scrubbed.txt").write_text("\n".join(removed) + "\n")

    venv = SHARED / "venv"
    if venv.exists():
        shutil.rmtree(venv)
    started = time.time()
    steps = [
        ("venv-created", [sys.executable, "-m", "venv", str(venv)],
         "a virtual environment could not be created"),
        ("target-stack-installed",
         [str(venv / "bin" / "pip"), "install", "--no-cache-dir", "--no-index",
          "--find-links", WHEELHOUSE, "-r", PINS],
         "the target wheel set could not be installed"),
        ("submission-installed",
         [str(venv / "bin" / "pip"), "install", "--no-cache-dir", "--no-index",
          "--find-links", WHEELHOUSE, str(source)],
         "pip install of the submitted source failed against the target "
         "wheelhouse -- either the project does not build, or it declares a "
         "dependency the target environment does not contain"),
    ]
    for cid, argv, why in steps:
        log = WORK / f"{cid}.log"
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=1800)
        log.write_text((proc.stdout or "") + (proc.stderr or ""))
        ok = proc.returncode == 0
        weight = 8.0 if cid == "submission-installed" else 1.0
        record(cid, ok, "" if ok else why,
               detail="" if ok else (proc.stdout or "") + (proc.stderr or ""),
               weight=weight, required=(cid == "submission-installed"))
        if not ok:
            return finish()

    py = str(venv / "bin" / "python")
    probe = (
        "import importlib.metadata as md, json, sys;"
        "dists=sorted(d.metadata['Name'].lower() for d in md.distributions()"
        " if d.metadata['Name']);"
        "print(json.dumps(dists))"
    )
    proc = subprocess.run([py, "-c", probe], capture_output=True, text=True)
    if proc.returncode != 0:
        record("closure-readable", False, "the installed closure is unreadable",
               detail=proc.stderr)
        return finish()
    record("closure-readable", True, weight=0.0)
    dists = json.loads(proc.stdout)
    (WORK / "closure.json").write_text(json.dumps(dists, indent=1))
    (SHARED / "closure.json").write_text(json.dumps(dists, indent=1))
    record("httpbin-is-a-distribution", "httpbin" in dists,
           "" if "httpbin" in dists else
           "the install produced no 'httpbin' distribution; the project was "
           "not installed as itself")

    install_declared_extras(py, source, dists)

    imported = subprocess.run(
        [py, "-c", "import httpbin, sys; sys.exit(0 if hasattr(httpbin,'app') else 3)"],
        capture_output=True, text=True, cwd="/var/tmp")
    record("app-imports-outside-the-tree", imported.returncode == 0,
           "" if imported.returncode == 0 else
           "importing httpbin:app from outside the source tree failed; the "
           "installed distribution does not stand on its own",
           detail=imported.stderr, weight=2.0)

    retirement_note(source)

    print(f"install finished in {time.time() - started:.1f}s")
    return finish()


def install_declared_extras(py: str, source: Path, dists: list[str]) -> None:
    """Also install the optional dependency groups the project declares.

    A published entry point is allowed to need something the library itself does
    not.  Upstream's does: ``Procfile`` and ``httpbin.bash`` both run gunicorn
    with a gevent worker, and gunicorn is in the ``mainapp`` extra rather than in
    the base requirements -- so the plain install above leaves the project unable
    to start by any means it publishes, and the whole suite then measures the
    harness's fallback line instead of the submission.

    What is read is the *built distribution's* ``Provides-Extra``, not the
    submitted ``pyproject.toml``: the question is what the thing that got
    installed says it offers.  Each extra is attempted on its own and a failure
    is recorded at weight 0, because an extra that cannot resolve offline is not
    a behavioural regression -- a ``test`` extra pinning tox is the normal case,
    and the graded install already succeeded without any of this.
    """
    if "httpbin" not in dists:
        return
    probe = (
        "import importlib.metadata as md, json;"
        "print(json.dumps(sorted(set("
        "md.metadata('httpbin').get_all('Provides-Extra') or []))))"
    )
    proc = subprocess.run([py, "-c", probe], capture_output=True, text=True)
    if proc.returncode != 0:
        record("extras-readable", True, weight=0.0,
               detail=f"could not read Provides-Extra: {proc.stderr}")
        return
    try:
        extras = json.loads(proc.stdout or "[]")
    except ValueError:
        extras = []
    if not extras:
        record("extras-declared", True, weight=0.0,
               detail="the built distribution declares no extras")
        return

    installed, refused = [], []
    for extra in extras:
        argv = [str(Path(py).with_name("pip")), "install", "--no-cache-dir",
                "--no-index", "--find-links", WHEELHOUSE,
                f"{source}[{extra}]"]
        run = subprocess.run(argv, capture_output=True, text=True, timeout=1800)
        (WORK / f"extra-{extra}.log").write_text(
            (run.stdout or "") + (run.stderr or ""))
        (installed if run.returncode == 0 else refused).append(extra)

    record("declared-extras-installed", True, weight=0.0,
           detail=f"declared {extras}; installed {installed}; "
                  f"could not resolve offline {refused}")
    print(f"extras: installed {installed or '-'}, unresolved {refused or '-'}")


def retirement_note(source: Path) -> None:
    """Record, without charging for it, whether the tree needs a retired wheel.

    A throwaway venv, and a wheelhouse that is only the target set. The graded
    install above resolved against the complete wheelhouse, so it says nothing
    about whether Flask was needed; this one cannot satisfy Flask at all, so a
    tree that still declares it fails here and names it.

    Not charged, and deliberately so: this stage grades behaviour against a
    corpus recorded from State A, which declares Flask itself. The retirement is
    stage 1's question and ``flask_retired`` owns it, with a gate failure scoring
    the whole submission zero before this stage runs.
    """
    if not os.path.isdir(WHEELHOUSE_PRUNED):
        record("pruned-wheelhouse-absent", True,
               f"{WHEELHOUSE_PRUNED} is not in this image, so whether the tree "
               f"needs a retired distribution was not observed", weight=0.0)
        return
    probe_venv = WORK / "pruned-venv"
    shutil.rmtree(probe_venv, ignore_errors=True)
    created = subprocess.run([sys.executable, "-m", "venv", str(probe_venv)],
                             capture_output=True, text=True)
    if created.returncode != 0:
        record("pruned-venv-created", True,
               "the probe venv could not be created, so the observation was "
               "skipped; nothing is scored on it", weight=0.0)
        return
    proc = subprocess.run(
        [str(probe_venv / "bin" / "pip"), "install", "--no-cache-dir",
         "--no-index", "--find-links", WHEELHOUSE_PRUNED, str(source)],
        capture_output=True, text=True, timeout=1800)
    text = (proc.stdout or "") + (proc.stderr or "")
    (WORK / "install-pruned-wheelhouse.log").write_text(text)
    if proc.returncode == 0:
        record("installs-without-retired-wheels", True,
               "the tree also installs against the agent's own wheelhouse, which "
               "carries no Flask, Werkzeug or flasgger wheel -- so nothing here "
               "needs the retired stack, which is the condition the agent worked "
               "under", weight=0.0)
    else:
        record("needs-retired-wheels", True,
               "the tree does NOT install against the agent's own wheelhouse. "
               "Something here still declares a distribution that wheelhouse "
               "withholds, so this is not a tree the agent could have installed "
               "offline. Not charged: this stage grades behaviour, and the "
               "retirement is stage 1's question.",
               detail=text, weight=0.0)
    shutil.rmtree(probe_venv, ignore_errors=True)


def finish() -> int:
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps({
        "checks": checks,
        "metadata": {"venv": str(SHARED / "venv"), "wheelhouse": WHEELHOUSE},
    }, indent=1))
    failed = [c["id"] for c in checks if c["verdict"] != "pass"]
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed"
          + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                       # a result must always exist
        record("install-module", False,
               f"the install module raised {type(exc).__name__}: {exc}",
               required=True)
        finish()
        raise
