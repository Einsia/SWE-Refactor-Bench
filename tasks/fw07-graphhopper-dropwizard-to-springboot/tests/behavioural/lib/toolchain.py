"""Shared plumbing for the four modules that are plain Python rather than pytest.

Seven modules compare recorded answers; those are pytest, and they never touch a
toolchain.  Four do something else — `build` compiles and exercises both trees,
`config` and `cli` launch processes with arguments and watch what they do to the
disk, and `own_tests` runs the repository's own suite — so they share a result
writer, a Maven environment, and a copy discipline.

Why the Maven environment matters more than it looks
----------------------------------------------------
Maven runs with `-o`, fully offline, against the local repository baked into this
image.  On THIS stage that repository is complete: it contains the Dropwizard
artefacts, because State A is one of the two sides being compared and a stage that
cannot compile its own reference cannot say anything about a submission.  The
AGENT's image is the pruned one — the Dropwizard jars are removed there, so a tree
that still depends on them does not resolve while the work is being done.  That is
a harder constraint than any grader check, and it is why this stage does not need
to re-litigate whether the framework left: stage 1 decides that over both trees'
source, and its gates zero the submission before this image runs.

Every artefact both stacks need is present, which is the property that makes the
comparison fair. A submission may add a Spring Boot dependency, a starter, an
embedded container — the offline repository was warmed with the union of both
closures, 2673 coordinates, precisely so that the target framework is reachable
without a network.

`-Dmaven.repo.local` is set explicitly rather than inherited.  A submitted tree
may ship its own `.mvn/maven.config` or a `settings.xml`, and a repository
location chosen by the tree being graded is a repository location a submission
could point at something it prepared.

`MAVEN_OPTS` caps the heap at 2g.  Not for speed: an OOM-killed Maven exits with a
signal and no useful log, which reads in a report like a build failure the
submission caused.

The version-range machinery is why `settings-offline.xml` names its mirror `central`
and why `_remote.repositories` files were deleted when the image was built.  Maven
records which repository id each artefact came from and refuses, offline, to use an
artefact whose provenance names a repository it is not currently configured with.
Under a mirror with any other id, every dependency in the tree becomes
unresolvable — with an error that talks about the artefact rather than about the
mirror.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from swerefactor.contract import submission_env

REPO = Path(os.environ.get("SRB_REPO", "/workspace/repo"))
WORK = Path(os.environ.get("SRB_WORK", "/tmp/srb-module"))
SHARED = Path(os.environ.get("SRB_SUITE_WORK", "/tmp/srb-fw07-suite"))
RESULT = Path(os.environ.get("SRB_RESULT", str(WORK / "result.json")))
SUITE = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"))
DATA = SUITE / "data"

MVN = shutil.which("mvn") or "/opt/maven/bin/mvn"
M2_REPO = os.environ.get("SRB_M2_REPO", "/root/.m2/repository")
OFFLINE_SETTINGS = os.environ.get("SRB_OFFLINE_SETTINGS",
                                  "/opt/srb/settings-offline.xml")

#: Where the build module leaves what the others read.
REFERENCE_TREE = SHARED / "reference-tree"
PROVENANCE = SHARED / "provenance.json"

# Build output a submitted tree might carry — a committed jar that find_app_jar
# would discover and grade in place of the submitted sources — is removed by
# `harness.maven.scrub_build_output`, called by the build module on both sides
# before either is built.  It lives there rather than here because it is part of
# how a tree is built, and because the same function is what stage 3 calls: two
# stages that scrubbed differently would grade two different artefacts.


def maven_env(**extra: str) -> dict[str, str]:
    # `submission_env` rather than `dict(os.environ)`: this environment is handed
    # to a build the submission controls, and the eight SRB_* contract names would
    # otherwise tell that build where the tests, the reference tree and the result
    # file live.  Not a boundary -- the build runs as root in this container and
    # can still go looking -- but a tree that hardcodes /tests/behavioural after
    # this is evidence rather than ambiguity, which is what stage 1 grades.
    env = submission_env()
    env["MAVEN_OPTS"] = (f"-Dmaven.repo.local={M2_REPO} -Xmx2g "
                         "-Djava.awt.headless=true")
    # A submitted tree's own JAVA_TOOL_OPTIONS would otherwise apply to Maven
    # itself and to every forked test JVM.
    env.pop("JAVA_TOOL_OPTIONS", None)
    env.pop("MAVEN_ARGS", None)
    env.update(extra)
    return env


def mvn_argv(goals: list[str], *, offline: bool = True,
             skip_tests: bool = True) -> list[str]:
    argv = [MVN, "-B", "-ntp", "--settings", OFFLINE_SETTINGS]
    if offline:
        argv.append("-o")
    if skip_tests:
        argv.append("-DskipTests")
    return argv + goals


DETAIL_CAP = 4000


def clip(detail: str, cap: int = DETAIL_CAP) -> str:
    """Bound `detail`, keeping BOTH ends and saying what went missing.

    This was a plain tail cut, which is right for a build log -- Maven's error
    lines are at the end -- and wrong for anything whose structure is at the
    front.  The launch ladder is the case that exposed it: `LaunchError` reports
    six rungs, that message runs to ~10KB, and a tail cut left exactly one rung in
    the report.  On this ladder the last rung is `no-config`, the fallback that is
    deliberately given no configuration, so its failure is guaranteed and
    uninformative -- while the rungs that got the port open, and the
    `BindException` that was the real reason, were cut away.

    A caller can fit its message under the cap (the launcher now does), but a
    report writer that silently discards the front of every long diagnostic is a
    trap for the next one that does not.  So the head is kept too, and the
    elision is stated rather than left to be inferred from a sentence that starts
    mid-word.
    """
    if len(detail) <= cap:
        return detail
    # Two thirds to the head: for a structured message the layout is there, and
    # for a log tail the last lines are usually the exception, which is shorter
    # than the context above it.
    head_len = (cap * 2) // 3
    tail_len = cap - head_len
    dropped = len(detail) - head_len - tail_len
    return (detail[:head_len]
            + f"\n\n  [... {dropped} character(s) elided from the middle of this "
              f"detail; the full text is in the module's log ...]\n\n"
            + detail[-tail_len:])


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
            entry["detail"] = clip(detail)
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
    proc = subprocess.run(argv, cwd=str(cwd) if cwd else None,
                          env=env or maven_env(),
                          capture_output=True, text=True, timeout=timeout)
    if log:
        (WORK / log).write_text((proc.stdout or "") + (proc.stderr or ""))
    return proc


def output_of(proc) -> str:
    return (proc.stdout or "") + (proc.stderr or "")


def grader_failure(report: Report, cid: str, message: str) -> int:
    """Abort the stage without attributing the failure to the submission.

    Used when something that belongs to the GRADER breaks: the reference tree not
    building, the reference not starting.  Exit 70 is the runner's
    infrastructure-failure code — distinct from 1, which means the submission
    failed checks.  The difference matters because a zero reported from a broken
    reference is a zero the submission did not earn, and it is indistinguishable
    from a real one once it is in a results table.
    """
    print(f"GRADER FAILURE: {message}", file=__import__("sys").stderr)
    report.record(cid, False, f"grader failure: {message}", weight=0.0)
    report.finish({"grader_failure": message})
    return 70
