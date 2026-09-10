"""Shared plumbing for the two modules that drive the Go toolchain.

The seven pytest modules measure a running binary.  These two measure what the
toolchain does with the tree: whether it resolves and compiles offline, and
whether the repository's own tests still pass.  So they are plain Python rather
than pytest, and they share a result writer, a Go environment and a repo copy.

Why the environment matters more than it looks
----------------------------------------------
``GOPROXY`` points at a file mirror with no network behind it, and on this stage
that mirror is COMPLETE: it serves every archive State A's own ``go.sum``
verified, the retired router's included.  That is deliberate and it is the
correction of a real defect.  State A imports the retired router, State A is the
oracle every expectation in ``data/golden-statea.json`` was recorded from, and a
stage built against a mirror that refuses that router cannot compile its own
oracle -- so it scores the baseline 0.000 and can never show that the suite it
backs is satisfiable.  Whether the retired dependency is gone is stage 1's
question, decided over both trees' source, and its gate zeroes the submission
before this image runs.

``$SRB_GOPROXY_PRUNED`` is a second mirror in the same image: the agent's own,
reproduced exactly, archive withheld.  Nothing that carries weight builds against
it.  The build module resolves the tree against it once, in a cold module cache,
and records the answer as a note.

The other 27 routers on the banned list are absent from both mirrors entirely, so
swapping gorilla/mux for chi fails to resolve rather than failing a check.

``GOFLAGS=-mod=mod`` is set because the alternative is worse than it looks.  With
``-mod=readonly`` -- Go's default since 1.16 -- a tree whose ``go.mod`` is stale
fails the build outright with "updates to go.mod needed".  On a behaviour stage
that is the wrong verdict twice over: a stale ``require`` line is untidiness, not
a behavioural regression, and the failure it produces is indistinguishable in a
log from a dependency that genuinely cannot be served.  Letting the build update
the graph in the *copy* keeps the two apart -- the tree still compiles, and
whether the manifest needed touching is recorded as a note instead.

``GOPRIVATE`` and ``GONOSUMDB`` are explicitly cleared.  A non-empty ``GOPRIVATE``
makes Go bypass the proxy for matching paths, and with a file mirror and no
network that is not a fallback, it is a hard failure with a confusing message.

``GOTOOLCHAIN=local`` because a ``go`` directive naming a version newer than the
installed toolchain otherwise makes Go try to download one.  There is no network,
so it fails -- but it fails as a proxy error, which reads like the dependency gate
again.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from swerefactor.contract import submission_env

REPO = Path(os.environ.get("SRB_REPO", "/workspace/repo"))
ORIGINAL = Path(os.environ.get("SRB_ORIGINAL", "/opt/original"))
WORK = Path(os.environ.get("SRB_WORK", "/tmp/srb-module"))
SHARED = Path(os.environ.get("SRB_SUITE_WORK", "/tmp/srb-shared"))
RESULT = Path(os.environ.get("SRB_RESULT", str(WORK / "result.json")))
SUITE = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"))
DATA = SUITE / "data"

GO = shutil.which("go") or "/usr/local/go/bin/go"
GOPROXY_ROOT = os.environ.get("SRB_GOPROXY_ROOT", "/opt/goproxy")

#: Where the build module publishes what the others read.  The name is the one
#: State A's own Makefile produces, so a submission that kept its Makefile and one
#: that builds by hand publish the same artefact under the same name.
BINARY = SHARED / "upload-server"
SOURCE = SHARED / "source"
BUILDINFO = SHARED / "buildinfo.txt"
MODULE_LIST = SHARED / "modules.txt"

#: Directories a submitted tree may contain that would make a broken build look
#: buildable, or an old build look like a new one.
ARTEFACTS = ("vendor", "bin", "dist", ".git")


def module_list(name: str) -> list[str]:
    """One module path per line from ``data/lockfiles/``; `#` starts a comment.

    Parsed the same way stage 1's ``srbscan._list_file`` parses the same two
    files, so the two stages cannot judge different lists from identical bytes.
    Read from ``data/lockfiles/`` because that is also where the mirror builder
    reads them: two copies of a list is how the mirror ends up stocked against
    one list while the graph is judged against the other.
    """
    path = DATA / "lockfiles" / name
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            out.append(stripped)
    return out


def go_env(**extra: str) -> dict[str, str]:
    """The offline build environment.  See the module docstring for each entry."""
    env = submission_env()
    env.update({
        "GOPROXY": f"file://{GOPROXY_ROOT}",
        "GOFLAGS": "-mod=mod",
        "GOSUMDB": "off",
        "GONOSUMDB": "",
        "GOPRIVATE": "",
        "GOTOOLCHAIN": "local",
        "CGO_ENABLED": "0",
        "GOWORK": "off",
    })
    env.update(extra)
    return env


def prepare_source() -> Path:
    """A clean copy of the submission, in the shared work directory.

    Nothing the agent left in the tree is trusted.  A committed ``vendor/`` would
    let a submission satisfy an import the mirror refuses to serve; a committed
    ``bin/`` would let a stale artefact answer for a build that no longer works.
    They are removed from a *copy* -- the graded tree itself is left exactly as
    submitted, because stage 3 compares against it and the report has to describe
    what was handed in.
    """
    SHARED.mkdir(parents=True, exist_ok=True)
    if SOURCE.exists():
        shutil.rmtree(SOURCE)
    shutil.copytree(REPO, SOURCE, symlinks=True,
                    ignore=shutil.ignore_patterns(*ARTEFACTS))
    return SOURCE


class Report:
    """A module's checks, and the JSON the runner reads them out of."""

    def __init__(self) -> None:
        self.checks: list[dict] = []

    def record(self, cid: str, ok: bool, summary: str = "", detail: str = "",
               weight: float = 1.0, required: bool = False) -> bool:
        entry = {"id": cid, "verdict": "pass" if ok else "fail", "weight": weight}
        if summary:
            entry["summary"] = summary
        if detail:
            entry["detail"] = detail[-4000:]
        if required:
            entry["required"] = True
        self.checks.append(entry)
        return ok

    def note(self, cid: str, summary: str) -> None:
        """An observation worth reading in the log that decides no weight."""
        self.record(cid, True, summary, weight=0.0)

    def finish(self, metadata: dict | None = None) -> int:
        RESULT.parent.mkdir(parents=True, exist_ok=True)
        RESULT.write_text(json.dumps(
            {"checks": self.checks, "metadata": metadata or {}}, indent=1))
        failed = [c["id"] for c in self.checks if c["verdict"] != "pass"]
        print(f"{len(self.checks) - len(failed)}/{len(self.checks)} checks passed"
              + (f"; failed: {', '.join(failed)}" if failed else ""))
        return 1 if failed else 0


def run(argv: list[str], *, cwd: Path | None = None, env: dict | None = None,
        timeout: float = 1800, log: str | None = None):
    """Run a command, tee its output to the module's work directory, return it."""
    WORK.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(argv, cwd=str(cwd) if cwd else None, env=env,
                          capture_output=True, text=True, timeout=timeout)
    if log:
        (WORK / log).write_text((proc.stdout or "") + (proc.stderr or ""))
    return proc


def output_of(proc) -> str:
    return (proc.stdout or "") + (proc.stderr or "")
