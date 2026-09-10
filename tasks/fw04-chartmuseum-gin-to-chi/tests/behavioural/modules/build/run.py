"""Rebuild the submission from its own source, offline, for everything after it.

This module is first and ``required``, for two reasons.

*It is a measurement.* "Resolves and compiles from its own source, offline" is
part of the migration rather than setup that happens before scoring starts. A tree
that does not compile cannot be measured for behaviour by anything after this.

The mirror this builds against is the COMPLETE one, and that is deliberate. State A
imports Gin, and State A is the oracle every expectation in ``data/`` was recorded
from, so a mirror that cannot compile Gin is a mirror that can never demonstrate
this suite is satisfiable -- measured: State A failed this module against the
pruned mirror and passed it against a complete one. Whether the tree still needs
an archive the agent was never given is recorded at the end of this module as an
unscored note, because it is stage 1's question and stage 1 answers it with a gate
that zeroes the whole submission.

*It is what every other module runs against.* The binary goes in
``$SRB_SUITE_WORK/chartmuseum``, is built once, and is measured by the fourteen
modules after it. Building per module would mean fifteen builds of a Go project;
building it in the image would mean grading an artifact the submission did not
produce.

Three things are published here for the modules that read them: the binary, the
linker's own module list from ``go version -m``, and the resolved build list from
``go list -m all``. Those two lists answer different questions, and neither answer
is scored here: the dependency question they bear on is stage 1's, owned by its
``gin_retired`` gate. They are published because ``own_tests`` reads them, and
because a finished run's reader can check that gate's verdict against them.
"""

from __future__ import annotations

import os
import shutil
import sys

import toolchain as tc

#: The version stamp State A's release carries. Passed to the linker so the
#: binary the CLI module asks `--version` about answers the way the release does.
LDFLAGS = "-w -X main.Version=0.15.0 -X main.Revision=460d8ec9"

#: The agent's own mirror, reproduced. Nothing is built against it; the unscored
#: observation at the end of this module is its only reader.
PRUNED = os.environ.get("SRB_GOPROXY_PRUNED", "/opt/goproxy-pruned")

rep = tc.Report()


def retirement_note(rep: tc.Report) -> None:
    """Record, without charging for it, whether the tree needs a retired archive.

    Two details make this observation mean anything.

    A COLD module cache: the graded build above unpacked every archive it needed
    into the shared one, and Go reads an unpacked module without consulting a proxy
    at all, so reusing that cache would report "needs nothing" even for a tree that
    imports Gin outright.

    A THROWAWAY copy of the source: ``-mod=mod`` lets a build rewrite go.mod, and
    this one is run deliberately against a mirror that cannot satisfy the graph.
    ``own_tests`` reads the real copy afterwards and must see the tree the graded
    build saw, not one this probe edited.
    """
    retired = [ln.strip() for ln in
               open(os.environ.get("SRB_RETIRED_MODULES",
                                   "/opt/srb/retired-modules.txt"))
               if ln.strip() and not ln.lstrip().startswith("#")]
    if not os.path.isdir(PRUNED):
        rep.note("pruned-mirror-absent",
                 f"{PRUNED} is not in this image, so whether the tree needs a "
                 f"retired archive was not observed. Nothing is scored on it.")
        return

    cold = tc.WORK / "pruned-modcache"
    copy = tc.WORK / "pruned-tree"
    out = tc.WORK / "pruned-out"
    for d in (cold, copy, out):
        shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(tc.SOURCE, copy, symlinks=True)
    out.mkdir(parents=True, exist_ok=True)
    probe = tc.run([tc.GO, "build", "-o", str(out) + "/", "./..."], cwd=copy,
                   env=tc.go_env(GOPROXY=f"file://{PRUNED}",
                                 MOD_PROXY_URL=f"file://{PRUNED}",
                                 GOMODCACHE=str(cold)),
                   timeout=2700, log="build-pruned-mirror.log")
    if probe.returncode == 0:
        rep.note("builds-without-retired-archive",
                 f"the tree also compiles against the agent's own mirror, in a "
                 f"cold module cache. That mirror serves no archive for "
                 f"{', '.join(retired)}, so nothing here needs the retired code -- "
                 f"which is the condition the agent worked under.")
    else:
        text = tc.output_of(probe)
        named = sorted(m for m in retired if m in text)
        rep.note("needs-retired-archive",
                 "the tree does NOT compile against the agent's own mirror in a "
                 "cold module cache"
                 + (f", and that mirror withholds {', '.join(named)}" if named else "")
                 + ". Something here still needs the retired code, so this is not "
                   "a tree the agent could have built offline. Not charged: this "
                   "stage grades behaviour, and the retirement is stage 1's "
                   "question.\n\n" + text)
    for d in (cold, copy, out):
        shutil.rmtree(d, ignore_errors=True)


def main() -> int:
    if not tc.REPO.is_dir():
        rep.record("submission-exists", False,
                   f"nothing to build: {tc.REPO} does not exist", required=True)
        return rep.finish()
    rep.note("submission-exists", f"building from {tc.REPO}")

    source = tc.prepare_source()
    if not (source / "go.mod").is_file():
        rep.record("go-module", False,
                   "there is no go.mod at the top of the submission, so the tree "
                   "is not a Go module and nothing can resolve", required=True)
        return rep.finish()
    rep.note("go-module", "go.mod present")

    env = tc.go_env()

    # 1. Resolve. Separated from the compile because the two fail for different
    #    reasons and a submission deserves to be told which: resolution fails when
    #    the module graph asks the mirror for something it does not carry, and the
    #    compile fails when the code is wrong.
    proc = tc.run([tc.GO, "mod", "download"], cwd=source, env=env,
                  timeout=1800, log="mod-download.log")
    if not rep.record(
            "dependencies-resolve", proc.returncode == 0,
            "the module graph could not be resolved against the offline mirror -- "
            "either it requires a module the target set does not contain, or it "
            "requires a version of one that was never published",
            detail=tc.output_of(proc), weight=2.0, required=True):
        return rep.finish()

    # 2. Compile everything, not just the command. A package that no longer builds
    #    is a package that was left behind, and `./cmd/chartmuseum` alone would not
    #    notice.
    proc = tc.run([tc.GO, "build", "./..."], cwd=source, env=env,
                  timeout=3600, log="build-all.log")
    if not rep.record(
            "every-package-compiles", proc.returncode == 0,
            "`go build ./...` failed: at least one package in the submission does "
            "not compile",
            detail=tc.output_of(proc), weight=4.0, required=True):
        return rep.finish()

    # 3. The graded binary.
    proc = tc.run([tc.GO, "build", "-v", "-o", str(tc.BINARY),
                   "--ldflags", LDFLAGS, "./cmd/chartmuseum"],
                  cwd=source, env=env, timeout=1800, log="build-binary.log")
    if not rep.record(
            "command-builds", proc.returncode == 0 and tc.BINARY.is_file(),
            "`go build ./cmd/chartmuseum` produced no binary, so there is nothing "
            "for the behavioural modules to measure",
            detail=tc.output_of(proc), weight=4.0, required=True):
        return rep.finish()
    tc.BINARY.chmod(0o755)

    # 4. What the linker actually put in it. Published rather than asserted, and it
    #    is the only fact in this stage that a source tree cannot present
    #    differently from the artifact built out of it.  Whether a retired module is
    #    still in there is stage 1's `gin_retired` gate -- required, reading both
    #    trees, and failing the submission outright -- so nothing here scores it.
    #    The file is written anyway: it is evidence a reader of a finished run can
    #    check that verdict against without rebuilding anything.
    proc = tc.run([tc.GO, "version", "-m", str(tc.BINARY)], cwd=source, env=env,
                  timeout=600, log="buildinfo.log")
    if rep.record("buildinfo-readable", proc.returncode == 0 and proc.stdout,
                  "`go version -m` could not read the binary it just built",
                  detail=tc.output_of(proc), weight=0.0):
        tc.BUILDINFO.write_text(proc.stdout)

    # 5. And the resolved build list, which is the same question asked of the
    #    module graph instead of the artifact.
    proc = tc.run([tc.GO, "list", "-m", "all"], cwd=source, env=env,
                  timeout=900, log="module-list.log")
    if rep.record("module-list-readable", proc.returncode == 0 and proc.stdout,
                  "`go list -m all` failed, so the resolved build list is unknown",
                  detail=tc.output_of(proc), weight=0.0):
        tc.MODULE_LIST.write_text(proc.stdout)

    # 6. It runs. A build that links is not a binary that starts, and every module
    #    after this one would otherwise report the same launch failure separately.
    proc = tc.run([str(tc.BINARY), "--version"], timeout=120, log="version.log")
    combined = tc.output_of(proc)
    rep.record("binary-runs", proc.returncode == 0 and "0.15.0" in combined,
               "the built binary does not run and report version 0.15.0",
               detail=combined, weight=2.0, required=True)

    # 7. And whether the tree still needs an archive the agent never had. Not
    #    charged, and deliberately so: everything above resolves against the
    #    COMPLETE mirror, because State A imports Gin and State A is the oracle
    #    every expectation here was recorded from. Whether the retirement happened
    #    is stage 1's question, answered there across the import graph, both
    #    manifests, vendor/ and the replace directives, with a gate failure scoring
    #    the whole submission zero before this stage runs at all.
    retirement_note(rep)

    return rep.finish({"binary": str(tc.BINARY), "source": str(tc.SOURCE),
                       "goproxy": tc.GOPROXY_ROOT,
                       "goproxy_pruned": PRUNED})


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                       # a result must always exist
        rep.record("build-module", False,
                   f"the build module raised {type(exc).__name__}: {exc}",
                   required=True)
        rep.finish()
        raise
