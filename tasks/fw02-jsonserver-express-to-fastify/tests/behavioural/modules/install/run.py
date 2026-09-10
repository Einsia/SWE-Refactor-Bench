"""Install the submission from source, offline, into a copy of its own tree.

Every other module in this stage boots what this one builds. Nothing else is
shared, which is why this module is declared first: if the submission cannot be
installed from what it delivered, there is no running server to compare against
State A. No module here declares ``required``, and none needs to: each module
downstream reports what it found with no server to talk to, and the stage pays only
for a submission that passed every scored check in every weighted module.

Why a copy
----------
Two reasons, and the second is the important one.

The delivered tree may contain a ``node_modules`` -- the agent had a mirror and
was told to use it, so it probably does. Installing on top of that would grade
whatever is already there against a ``package.json`` that might not resolve at
all. So the copy drops everything an install or a build produces and starts from
source.

And stage 3 reads the tree as submitted. An adversary is asked to find an input
these two trees disagree about, which means it has to see what was actually
delivered, not what a grader left behind after installing into it. Keeping the
mutation in a copy is what makes those two uses of one directory compatible.

What the mirror does and does not decide
---------------------------------------
The install is offline, but *not* against the agent's mirror, and the difference
decides something. Two mirrors ship in this image and the Dockerfile says why:
``/opt/npm-registry`` is COMPLETE and ``/opt/npm-registry-pruned`` reproduces the
agent's, with the 54 retired distributions removed. ``suite.toml`` points this
stage at the complete one.

It has to. State A is the repository every recorded expectation came from and it
declares ``express``, so the pruned mirror cannot resolve State A's own closure --
and since every module here boots what this one installs, it would take the whole
stage with it. The pruned copy is built and asserted at image-build time, which is
what proves the agent's environment was really missing ``express``, and it is not
what grades.

So the retired list is NOT enforced in this module. A submission that migrated its
server to Fastify but left ``express`` in ``package.json`` installs cleanly here
and can score full marks in this stage: nothing here grades what the manifest
declares, and the five checks that would notice the old stack on the wire or in a
startup banner all carry ``weight = 0``, because whether the migration happened is
stage 1's question rather than this stage's. What answers it is the agent's pruned
mirror during the agent phase, and stage 1's ``old_stack_retired`` gate when the
submission is graded.

Do not "fix" this by pointing the stage at the pruned mirror, and do not weaken
``old_stack_retired`` on the belief that npm already covers it. Nothing in this
stage does.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SUBMISSION = Path(os.environ.get("SRB_REPO", "/workspace/repo"))
SHARED = Path(os.environ["SRB_SUITE_WORK"])
WORK = Path(os.environ.get("SRB_WORK", "/tmp/srb"))
RESULT = Path(os.environ["SRB_RESULT"])
SRB_NPM = os.environ.get("SRB_NPM", "/usr/local/bin/srb-npm")

BUILD = SHARED / "build"

#: Anything an install or a build writes. Dropped from the copy so the install
#: below is genuinely from source. Note ``dist``/``build`` are *not* here: a
#: submission may legitimately commit compiled output, and whether it did is a
#: question about the repository, which is stage 1's.
GENERATED = (
    "node_modules",
    ".npm",
    ".cache",
    ".yarn",
    ".pnpm-store",
    ".git",
    ".nyc_output",
    "coverage",
)

CHECKS: list[dict] = []


#: The three below are the Node-crash branch of ``swerefactor.pytest_module``'s rule:
#: a continuation that is only a location (``file:///app/src/cli/index.js:10``)
#: stands in for the exception printed four lines under it.  It does not fire on
#: anything *this* module writes -- npm's failures are log tails, not tracebacks --
#: and it is here anyway, because the docstring below claims this is the same rule
#: as infra's, and a copy that quietly stops being one is how the next reader gets
#: misled.
_BARE_LOCATION = re.compile(r"^\S+:\d+(?::\d+)?$")
_CAUSE_LINE = re.compile(r"^[A-Za-z_][\w.]*: \S")
_CAUSE_WINDOW = 6


def headline(detail: str, limit: int = 300) -> str:
    """The first line of ``detail``, plus the second when the first only announces.

    The same rule as ``swerefactor.pytest_module._headline`` and as the replay module's
    copy, kept local because a module is standalone by contract.  Without it this
    module's checks -- including the one that decides whether the offline install
    resolved at all -- carried an empty summary, and the stage-2 report prints only
    the module table, so a failed install had no text anywhere a reader looks.
    """
    lines = [ln for ln in (detail or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    head = lines[0].strip()
    if head.endswith(":") and len(lines) > 1:
        cont = lines[1].strip()
        if _BARE_LOCATION.match(cont):
            for ln in lines[2:2 + _CAUSE_WINDOW]:
                if _CAUSE_LINE.match(ln.strip()):
                    cont = f"{ln.strip()} (at {cont})"
                    break
        head = f"{head} {cont}"
    return head[:limit]


def record(check_id: str, verdict: str, detail: str, **extra) -> None:
    entry = {"id": check_id, "verdict": verdict,
             "summary": headline(detail), "detail": detail}
    entry.update(extra)
    CHECKS.append(entry)
    print(f"[{verdict:5s}] {check_id}: {detail.splitlines()[0][:160]}")


def finish() -> None:
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps({"checks": CHECKS}, indent=1))
    failed = [c["id"] for c in CHECKS if c["verdict"] in ("fail", "error")]
    print(f"\n{len(CHECKS)} checks, {len(failed)} not passing"
          + (f": {', '.join(failed)}" if failed else ""))
    raise SystemExit(0)


def run(argv: list[str], cwd: Path, log: Path,
        timeout: int = 900) -> tuple[int, str]:
    """Run a command, tee its output to a log, return (rc, tail)."""
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with log.open("wb") as fh:
        fh.write(f"+ cd {cwd}\n+ {' '.join(argv)}\n\n".encode())
        fh.flush()
        try:
            proc = subprocess.run(argv, cwd=str(cwd), stdout=fh,
                                  stderr=subprocess.STDOUT, timeout=timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            fh.write(f"\n[timed out after {timeout}s]\n".encode())
            rc = 124
        except OSError as exc:
            fh.write(f"\n[could not run: {exc}]\n".encode())
            rc = 127
    elapsed = time.monotonic() - started
    text = log.read_text(errors="replace").splitlines()
    tail = "\n".join(text[-40:])
    print(f"    {' '.join(argv)} -> rc={rc} in {elapsed:.1f}s ({log.name})")
    return rc, tail


def copy_source() -> list[str]:
    """Copy the delivered tree, dropping everything a build produces."""
    if BUILD.exists():
        shutil.rmtree(BUILD)
    dropped: list[str] = []

    def ignore(directory: str, names: list[str]) -> set[str]:
        out = set()
        for name in names:
            if name in GENERATED:
                out.add(name)
                rel = Path(directory).relative_to(SUBMISSION) / name
                dropped.append(str(rel))
            elif name.endswith(".tgz"):
                out.add(name)
                rel = Path(directory).relative_to(SUBMISSION) / name
                dropped.append(str(rel))
        return out

    shutil.copytree(SUBMISSION, BUILD, symlinks=True, ignore=ignore)
    return sorted(dropped)


def main() -> None:
    logs = WORK / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    # -- the copy ----------------------------------------------------------
    if not (SUBMISSION / "package.json").is_file():
        record("copy", "fail",
               f"no package.json at {SUBMISSION}: there is no Node package here "
               f"to install, so nothing in this stage can run.")
        finish()

    try:
        dropped = copy_source()
    except OSError as exc:
        record("copy", "error", f"could not copy the submission: {exc}")
        finish()

    record("copy", "pass",
           f"copied the submission to {BUILD}"
           + (f"; dropped {len(dropped)} generated path(s): "
              f"{', '.join(dropped[:8])}" if dropped else
              "; nothing generated to drop"),
           dropped=dropped)

    # The submission's own claims, kept for diagnosis. A disagreement between
    # package.json and the lockfile shows up as an install failure below, and
    # having both in the log is what makes that failure readable.
    try:
        manifest = json.loads((BUILD / "package.json").read_text())
    except (OSError, ValueError) as exc:
        record("manifest", "fail",
               f"package.json is not readable JSON: {exc}. npm cannot install "
               f"this and nothing here can boot it.")
        finish()

    (logs / "submitted-package.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True))
    has_lock = (BUILD / "package-lock.json").is_file()
    if has_lock:
        shutil.copy2(BUILD / "package-lock.json",
                     logs / "submitted-package-lock.json")

    deps = sorted((manifest.get("dependencies") or {}).items())
    record("manifest", "pass",
           f"package.json declares {len(deps)} runtime dependenc(ies)"
           f"{' and ships a lockfile' if has_lock else ' with no lockfile'}",
           dependencies=dict(deps),
           lockfile=has_lock)

    # -- the install -------------------------------------------------------
    # `npm ci` when there is a lockfile: that is the reproducible path a
    # deployment would take, and it is the stricter of the two -- it refuses a
    # lockfile that disagrees with package.json rather than quietly fixing it.
    argv = [SRB_NPM, "ci" if has_lock else "install"]
    rc, tail = run(argv, BUILD, logs / "install.log", timeout=1800)
    if rc != 0:
        record("offline-install", "fail",
               f"`{' '.join(argv[1:])}` failed against the offline mirror "
               f"(rc={rc}). The mirror carries the target stack and not the "
               f"retired one, so a dependency it cannot resolve is a dependency "
               f"the migration was supposed to remove.\n\n{tail}",
               returncode=rc)
        finish()
    record("offline-install", "pass",
           f"`{' '.join(argv[1:])}` resolved the declared closure offline")

    installed = BUILD / "node_modules"
    if not installed.is_dir():
        record("installed-tree", "fail",
               "the install reported success but produced no node_modules; "
               "there is nothing to boot.")
        finish()
    count = sum(1 for _ in installed.glob("*/package.json")) + \
        sum(1 for _ in installed.glob("@*/*/package.json"))
    record("installed-tree", "pass",
           f"{count} package(s) installed under node_modules")

    # -- the build, if the package declares one ----------------------------
    scripts = manifest.get("scripts") or {}
    if "build" in scripts:
        rc, tail = run(["npm", "run", "build", "--loglevel", "warn"],
                       BUILD, logs / "build.log", timeout=900)
        if rc != 0:
            # Not fatal here. A submission whose build fails may still boot from
            # sources it committed, and whether it boots is the next module's
            # question, asked by starting it. Failing the stage on a build script
            # would be grading a log line instead of a server.
            record("build", "fail",
                   f"`npm run build` failed (rc={rc}). Recorded rather than "
                   f"fatal: whether the service starts is decided by starting "
                   f"it.\n\n{tail}",
                   returncode=rc)
        else:
            record("build", "pass", "`npm run build` succeeded")
    else:
        record("build", "skip",
               "the package declares no build script; nothing to build")

    # -- the thing every other module actually needs ------------------------
    launcher = BUILD / os.environ.get("SRB_LAUNCHER", "serve.sh")
    if launcher.is_file():
        record("launcher-present", "pass",
               f"{launcher.name} is present in the installed copy")
    else:
        # Also not fatal, and deliberately so: the launcher contract is a
        # question about the repository. Stage 1 asks it against the source, and
        # `entrypoint` here asks the observable half by trying to start the
        # service. Recorded so that a stage where nothing boots has one line
        # explaining why.
        record("launcher-present", "fail",
               f"{launcher.name} is not in the delivered tree. Every module here "
               f"starts the service through it, so they will all report a server "
               f"that did not start.")

    finish()


if __name__ == "__main__":
    sys.exit(main())
