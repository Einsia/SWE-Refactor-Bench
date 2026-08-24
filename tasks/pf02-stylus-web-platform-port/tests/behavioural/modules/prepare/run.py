"""Stage the submitted tree, install its dependencies, publish a ledger.

This module runs first and is ``required``, for two reasons.

*It is what every other module runs against.*  The staged tree goes in
``$SRB_SUITE_WORK/tree`` and the eleven dimension modules address the submission
through it.  Doing the copy per module would mean eleven copies of a tree with a
dependency install in each; measuring ``$SRB_REPO`` in place would mean writing
inside the artifact being graded, and loading whatever the agent happened to leave
in ``node_modules``.

*The install is itself a measurement.*  It runs offline from the submission's own
``package-lock.json`` against the same cache the agent was given, which holds
State A's five dependencies and nothing else.  A submission that added a sixth
dependency does not install here at all -- that is the no-network rule being
enforced rather than merely stated.

What is required and what is not is the whole design of this module:

  * ``tree-staged`` is required.  Without a copy there is nothing to measure and
    the stage stops, which is the correct report for an empty submission.
  * ``dependencies-installed`` is scored but **not** required.  ``src/core/``
    cannot import a package -- the realm that hosts it resolves relative
    specifiers only -- so a submission with a working core and a stale lockfile
    should lose the Node-side modules and keep the rest.  Making this required
    would zero a stage that still had two thirds of its evidence intact.

``node_modules`` is discarded rather than audited.  A submission that vendored a
complete second compiler in there therefore ships nothing: the install that
follows is from the lockfile, and the lockfile is checked against the cache.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from swerefactor.contract import submission_env

REPO = Path(os.environ.get("SRB_REPO", "/workspace/repo"))
WORK = Path(os.environ.get("SRB_WORK", "/var/tmp/pf02-prepare"))
SHARED = Path(os.environ["SRB_SUITE_WORK"])
RESULT = Path(os.environ["SRB_RESULT"])
STATE_A = Path(os.environ.get("PF03_STATE_A", "/opt/state-a"))
NPM_CACHE = os.environ.get("NPM_CONFIG_CACHE", "/opt/npm-cache")
INSTALL_TIMEOUT_SEC = float(os.environ.get("PF03_INSTALL_TIMEOUT", "1200"))

TREE = SHARED / "tree"
LEDGER = SHARED / "prepare.json"

#: Names dropped from the copy, at the top level only.  Every one of them is a
#: top-level concept -- an installed dependency tree, a VCS directory, a cache --
#: and matching them at any depth would delete test input: upstream ships
#: `test/cases/import.lookup/node_modules` on purpose, because one of its own cases
#: imports from it.
#:
#: The graded tree itself is left exactly as it was handed in.  Stage 1 and stage 3
#: both read it, and their report has to describe the submission rather than our
#: copy of it.
DISCARD = ("node_modules", ".git", ".npm", ".cache", "coverage", ".nyc_output")

checks: list[dict] = []


def record(cid: str, ok: bool, summary: str = "", detail: str = "",
           weight: float = 1.0, required: bool = False) -> bool:
    entry: dict = {"id": cid, "verdict": "pass" if ok else "fail", "weight": weight}
    if summary:
        entry["summary"] = summary
    if detail:
        entry["detail"] = detail[-4000:]
    if required:
        entry["required"] = True
    checks.append(entry)
    return ok


def stage() -> list[str]:
    """Copy the submission, dropping the top-level directories listed in DISCARD."""
    if TREE.exists():
        shutil.rmtree(TREE)
    dropped: list[str] = []
    top = REPO.resolve()

    def ignore(directory: str, names: list[str]) -> set[str]:
        if Path(directory).resolve() != top:
            return set()
        here = {n for n in names if n in DISCARD}
        dropped.extend(sorted(here))
        return here

    shutil.copytree(REPO, TREE, symlinks=True, ignore=ignore)
    return dropped


def install() -> tuple[bool, str, str]:
    """`npm ci --offline` in the staged tree.  Returns (ok, reason, log).

    ``--ignore-scripts`` because an install is not a licence to run arbitrary
    code from the tree being graded.  State A's dependencies have no install
    scripts, so nothing a correct submission needs is lost by refusing them.
    """
    argv = ["npm", "ci", "--offline", "--no-audit", "--no-fund", "--ignore-scripts"]
    try:
        proc = subprocess.run(
            argv, cwd=str(TREE), capture_output=True, text=True,
            timeout=INSTALL_TIMEOUT_SEC,
            env=submission_env(extra={"NPM_CONFIG_CACHE": NPM_CACHE, "CI": "1"}),
        )
    except subprocess.TimeoutExpired as exc:
        return False, (f"npm ci did not finish within "
                       f"{INSTALL_TIMEOUT_SEC:g}s"), str(exc.output or "")
    except OSError as exc:
        return False, f"npm could not be started: {exc}", ""
    log = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return True, "", log
    return False, (
        f"npm ci --offline exited {proc.returncode}. Either package-lock.json is "
        f"out of step with package.json, or it asks for a package the offline "
        f"cache does not contain -- the cache holds exactly the dependencies "
        f"State A declared."
    ), log


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    SHARED.mkdir(parents=True, exist_ok=True)
    ledger: dict = {
        "tree": str(TREE),
        "submitted": str(REPO),
        "state_a": str(STATE_A),
        "dependencies_installed": False,
        "install_error": "",
        "lockfile_present": False,
    }

    if not REPO.is_dir():
        record("tree-staged", False,
               f"nothing to measure: {REPO} does not exist", required=True)
        return finish(ledger)

    started = time.time()
    try:
        dropped = stage()
    except OSError as exc:
        record("tree-staged", False,
               f"the submission could not be copied to {TREE}: {exc}",
               required=True)
        return finish(ledger)
    (WORK / "discarded.txt").write_text("\n".join(dropped) + "\n", encoding="utf-8")
    record("tree-staged", True,
           f"copied in {time.time() - started:.1f}s, dropped {len(dropped)} "
           f"director{'y' if len(dropped) == 1 else 'ies'}",
           weight=1.0, required=True)
    ledger["discarded"] = dropped

    # package.json has to parse before npm is asked to read it, so that "the
    # manifest is not JSON" reads as itself rather than as an install failure.
    manifest = TREE / "package.json"
    if not manifest.is_file():
        record("manifest-present", False,
               "there is no package.json in the submission, so its dependencies "
               "cannot be installed and nothing that needs them can run")
        return finish(ledger)
    record("manifest-present", True, weight=0.0)
    try:
        parsed = json.loads(manifest.read_text(encoding="utf-8"))
        record("manifest-parses", True, weight=1.0)
    except (ValueError, OSError) as exc:
        record("manifest-parses", False, f"package.json does not parse: {exc}")
        return finish(ledger)
    ledger["package_name"] = str(parsed.get("name", ""))
    ledger["declared_dependencies"] = sorted((parsed.get("dependencies") or {}).keys())

    lock = TREE / "package-lock.json"
    ledger["lockfile_present"] = lock.is_file()
    if not lock.is_file():
        record("lockfile-present", False,
               "there is no package-lock.json, so there is no offline install: "
               "anything the submission reaches through a bare specifier will "
               "fail to resolve. The realm-hosted core does not use one and is "
               "measured normally.",
               weight=1.0)
        return finish(ledger)
    record("lockfile-present", True, weight=0.0)

    ok, reason, log = install()
    (WORK / "npm-ci.log").write_text(log, encoding="utf-8")
    ledger["dependencies_installed"] = ok
    ledger["install_error"] = reason
    # Weighted above the checks around it and deliberately not required: it gates
    # the Node-side modules by making them fail on their own, not this stage.
    record("dependencies-installed", ok, reason, detail="" if ok else log,
           weight=4.0)
    if ok:
        installed = sorted(
            p.name for p in (TREE / "node_modules").iterdir()
            if p.is_dir() and not p.name.startswith(".")
        ) if (TREE / "node_modules").is_dir() else []
        ledger["installed_top_level"] = installed
        print(f"installed {len(installed)} top-level packages")

    return finish(ledger)


def finish(ledger: dict) -> int:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(ledger, indent=1) + "\n", encoding="utf-8")
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps({
        "checks": checks,
        "metadata": {"tree": str(TREE), "ledger": str(LEDGER),
                     "npm_cache": NPM_CACHE},
    }, indent=1) + "\n", encoding="utf-8")
    failed = [c["id"] for c in checks if c["verdict"] != "pass"]
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed"
          + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                      # a result must always exist
        record("prepare-module", False,
               f"the prepare module raised {type(exc).__name__}: {exc}",
               required=True)
        finish({"tree": str(TREE), "dependencies_installed": False,
                "install_error": f"prepare crashed: {exc}"})
        raise
