"""Shared plumbing for the four modules that drive the Go toolchain.

The eleven pytest modules measure a running binary. These four measure the
toolchain's answers about the tree it was built from -- what the linker put in the
binary, whether every release platform still compiles, whether the repository's own
tests still pass -- so they are plain Python rather than pytest, and they share a
result writer, a Go environment and a repo copy.

Why the environment matters more than it looks
----------------------------------------------
``GOPROXY`` points at a file mirror with no network behind it, and the mirror is
pruned: the three retired modules keep their ``.mod`` so that ``go mod tidy`` can
still resolve the graph, and have no ``.zip``, so nothing that imports them can be
compiled. That is the dependency gate, and it is enforced by the toolchain's own
resolver rather than by anything here.

``MOD_PROXY_URL`` has to be set for the same reason and it is easy to miss. The
repository's Makefile does ``export GOPROXY=$(MOD_PROXY_URL)`` in its build
targets, so leaving it unset exports an *empty* ``GOPROXY``, which Go reads as "use
the default" -- the network, in a container that has none. The repo's own CI sets
it; so does every module here.

``GOPRIVATE`` and ``GONOSUMDB`` are explicitly cleared. A non-empty ``GOPRIVATE``
makes Go bypass the proxy for matching paths, and with a file mirror and no network
that is not a fallback, it is a hard failure with a confusing message.
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

#: Where the build module publishes what the others read.
BINARY = SHARED / "chartmuseum"
SOURCE = SHARED / "source"
BUILDINFO = SHARED / "buildinfo.txt"
MODULE_LIST = SHARED / "modules.txt"

#: Directories a submitted tree may contain that would make a broken build look
#: buildable, or an old build look like a new one.
ARTEFACTS = ("vendor", "bin", "testbin", ".git")


def go_env(**extra: str) -> dict[str, str]:
    """The offline build environment, as the repository's own CI shapes it."""
    env = submission_env()
    env.update({
        "GOPROXY": f"file://{GOPROXY_ROOT}",
        "MOD_PROXY_URL": f"file://{GOPROXY_ROOT}",
        "GOFLAGS": "-mod=mod",
        "GOSUMDB": "off",
        "GONOSUMDB": "",
        "GOPRIVATE": "",
        "GOTOOLCHAIN": "local",
        "GOFLAGS_EXTRA": "",
        "CGO_ENABLED": "0",
    })
    env.update(extra)
    return env


def prepare_source() -> Path:
    """A clean copy of the submission, in the shared work directory.

    Nothing the agent left in the tree is trusted. A committed ``vendor/`` would
    let a submission satisfy an import the mirror refuses to serve; a committed
    ``bin/`` would let a stale artifact answer for a build that no longer works.
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
        """Record one check.  `summary` says why it FAILED, so a pass drops it.

        A module writes the summary once, at the call site, worded as the reason
        the check failed -- 28 of this task's 32 call sites do, and that is the
        convention rather than an oversight.  Keeping it on a pass therefore
        publishes a false statement: measured on the full-marks identity run, 28
        weighted passing checks asserted their own failure, including
        `build/binary-runs` verdict=pass summary="the built binary does not run
        and report version 0.15.0", and all seven `own_tests/tests-survive-*`
        passing while saying "had tests in State A and now has none".  A stage-3
        reviewer reads these summaries.

        Dropping it here rather than at the call sites is deliberate: 28 edits can
        each be got wrong, and the 29th check a future module adds would reproduce
        the bug.  `detail` is kept on a pass -- it is evidence, not an assertion,
        and modules already gate it on failure where it would mislead.
        """
        entry = {"id": cid, "verdict": "pass" if ok else "fail", "weight": weight}
        if summary and not ok:
            entry["summary"] = summary
        if detail:
            entry["detail"] = detail[-4000:]
        if required:
            entry["required"] = True
        self.checks.append(entry)
        return ok

    def note(self, cid: str, summary: str) -> None:
        """An observation worth reading in the log that decides no weight.

        Built by hand rather than through ``record``, which drops a passing
        check's summary: a note is nothing BUT its summary, so routing it through
        record would erase every note in the task while each one still appeared in
        the JSON as a weight-0 entry with no text -- present, and unreadable.
        """
        # The marker goes in `metadata`, not at the top level: the runner builds
        # its Check through `Check.from_dict`, which reads named keys and drops
        # everything else, so a top-level "note" would be visible in this module's
        # own JSON and gone from the merged report.  `metadata` it does read.
        self.checks.append({"id": cid, "verdict": "pass", "weight": 0.0,
                            "summary": summary, "metadata": {"note": True}})

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
