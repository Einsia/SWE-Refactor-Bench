#!/usr/bin/env python3
"""Resolve and compile the submission, offline, and publish what it produced.

This module runs first, and what it does is a PRECONDITION rather than a verdict:
the eight modules after it send HTTP requests to the binary published here, so
without one there is no behaviour to observe.  Nothing in this file grades the
dependency graph.

`$SRB_GOPROXY_ROOT` is the COMPLETE mirror.  It serves every archive State A's own
go.sum verified, the retired router's included, and that is deliberate: State A
imports that router, State A is the oracle every expectation in
`data/golden-statea.json` was recorded from, and a stage that cannot build the
oracle cannot demonstrate the suite it backs is satisfiable.  Feeding State A in
as a submission has to score 1.000 here.  Building against the pruned mirror
instead would score it 0.000 on a replay of its own answers -- not on any
behavioural disagreement, but because the oracle imports the module that mirror
withholds.

Whether the retired dependency is actually gone is stage 1's question, asked over
both trees' source with a reviewer, and a stage-1 gate failure scores the whole
submission zero before this image runs.  The authoring constraint is stronger than
either: the AGENT's mirror is pruned, so a tree that still imports the retired
router cannot be compiled by the agent while the work is being done.

Four scored checks, worth 5.0 pooled weight between them, and every one of them a
precondition: the build succeeded, the binary is executable, it starts and prints
its own usage, and the flags the recorded profiles launch with are still declared.
Any one of them failing closes stage 2, because the stage pays only a submission
whose every weighted module passed every scored check -- there is no ranking among
them to declare and nothing a module can say about itself to change that.

Everything this module has to say about the dependency graph is a `rep.note` --
weight 0.0, verdict pass, unable to fail the module or move the stage's rate:

  * the resolved build list, so the report can be read against it;
  * what the linker recorded in the binary, which no source edit talks out of;
  * whether the tree still needs an archive the agent's own mirror withholds,
    measured against `$SRB_GOPROXY_PRUNED` in a cold module cache.

They are written down because the toolchain's answer to stage 1's question is
worth having in the run's artefacts.  None of them is worth a point here.
"""
from __future__ import annotations

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))) + "/lib")

import toolchain as tc  # noqa: E402

#: The agent's own mirror, reproduced in this image and built against by nothing
#: that carries weight.  Read once, for the second observation below.
PRUNED = os.environ.get("SRB_GOPROXY_PRUNED", "/opt/goproxy-pruned")


def _covers(prefix: str, path: str) -> bool:
    """Module-path containment, the way Go means it: `x/y` covers `x/y/v2`."""
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def _record_dependency_observations(rep, env) -> None:
    """Mechanical facts about the dependency graph: published, and NOT scored.

    Every call in here is ``rep.note`` -- weight 0.0, verdict pass, cannot fail the
    module and cannot move the stage's rate.  That is the point.  Stage 1 decides
    whether the migration happened, over both trees' source and with a reviewer.
    This stage grades behaviour.  What is useful is that the toolchain's own answer
    to stage 1's question is written down in the run's artefacts rather than left
    for someone to reconstruct, so the facts are recorded where they cost nothing.

    Weighting them here would put the two axes on one number, and it fails in the
    direction that matters: State A imports the retired router, so a weighted
    dependency assertion scores the oracle down on a replay of its own answers.
    """
    retired = tc.module_list("retired-modules.txt")
    banned = tc.module_list("banned-routers.txt")

    # ---- the resolved build list, for the report to be readable against.
    lst = tc.run([tc.GO, "list", "-deps",
                  "-f", "{{if .Module}}{{.Module}}{{end}}", "./..."],
                 cwd=tc.SOURCE, env=env, timeout=900, log="go-list-deps.log")
    tc.MODULE_LIST.write_text(tc.output_of(lst))
    rep.note("build-list-published",
             f"resolved build list written to {tc.MODULE_LIST.name}"
             if lst.returncode == 0 else
             f"`go list -deps ./...` exited {lst.returncode}; "
             f"{tc.MODULE_LIST.name} holds whatever it managed to print")

    # ---- what the linker actually put in the binary.
    #
    # `go version -m` is the linker's record, not a claim about source.  It lists
    # only modules whose code was linked, so it can say something an import scan
    # cannot: a module reached through a replace directive, or one still linked
    # while the import that pulls it in has been moved out of sight.
    vm = tc.run([tc.GO, "version", "-m", str(tc.BINARY)], env=env, timeout=300,
                log="buildinfo.log")
    info = tc.output_of(vm)
    tc.BUILDINFO.write_text(info)
    linked = [f[1] for f in (ln.split() for ln in info.splitlines())
              if len(f) > 1 and f[0] in ("dep", "=>")]
    flagged = sorted({p for p in linked
                      if any(_covers(m, p) for m in retired + banned)})
    if flagged:
        rep.note("linked-router-observed",
                 f"the linker recorded {', '.join(flagged)} in the binary. Not "
                 f"charged here -- this stage grades behaviour -- but it is the "
                 f"toolchain's own answer to stage 1's question, and stage 1's "
                 f"gate is where that answer counts.\n\n{info}")
    else:
        rep.note("no-router-linked",
                 f"the linker recorded no retired or banned module in the binary; "
                 f"{len(linked)} module(s) linked.\n\n{info}")

    # ---- whether the tree still needs an archive the agent never had.
    #
    # Two details make this observation mean anything.
    #
    # A COLD module cache: the build above unpacked every archive it needed into
    # the shared one, and Go reads an unpacked module without consulting a proxy at
    # all, so reusing that cache would report "needs nothing" for a tree that
    # imports the retired router outright.
    #
    # A THROWAWAY copy of the source: `-mod=mod` lets a build rewrite go.mod, and
    # this build is deliberately run against a mirror that cannot satisfy the
    # graph.  `own_tests` reads the same copy afterwards and must see the tree the
    # real build saw, not one this probe edited.
    cold = tc.WORK / "pruned-modcache"
    copy = tc.WORK / "pruned-tree"
    out = tc.WORK / "pruned-out"
    for d in (cold, copy, out):
        shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(tc.SOURCE, copy, symlinks=True)
    out.mkdir(parents=True, exist_ok=True)
    probe = tc.run([tc.GO, "build", "-buildvcs=false", "-o", str(out) + "/", "./..."],
                   cwd=copy,
                   env=tc.go_env(GOPROXY=f"file://{PRUNED}", GOMODCACHE=str(cold)),
                   timeout=2700, log="build-pruned-mirror.log")
    if probe.returncode == 0:
        rep.note("builds-without-retired-archive",
                 f"the tree also compiles against the agent's own mirror, in a "
                 f"cold module cache. That mirror serves no archive for "
                 f"{', '.join(retired) or 'the retired module'}, so nothing in "
                 f"this tree needs the retired code -- which is the condition the "
                 f"agent worked under.")
    else:
        text = tc.output_of(probe)
        named = sorted({m for m in retired
                        if m in text or any(_covers(m, p) for p in linked)})
        rep.note("needs-retired-archive",
                 "the tree does NOT compile against the agent's own mirror in a "
                 "cold module cache"
                 + (f", and that mirror withholds {', '.join(named)}"
                    if named else "")
                 + ". Something here still needs the retired module's code, so "
                   "this is not a tree the agent could have built offline. Not "
                   "charged: this stage grades behaviour, and the retirement is "
                   "stage 1's question.\n\n" + text)
    for d in (cold, copy, out):
        shutil.rmtree(d, ignore_errors=True)


def main() -> int:
    rep = tc.Report()
    src = tc.prepare_source()
    env = tc.go_env()

    removed = [d for d in tc.ARTEFACTS if (tc.REPO / d).exists()]
    if removed:
        rep.note("artefacts-removed",
                 f"removed from the build copy: {', '.join(removed)} -- a "
                 f"committed vendor/ would satisfy an import the mirror refuses "
                 f"to serve, and a committed bin/ would let a stale artefact "
                 f"answer for a build that no longer works")

    # The build target: the module root, which is where State A's `main` lives.
    #
    # -buildvcs=false because State A ships no .git and a submission's own working
    # habits must not decide whether this module can run.  With VCS stamping left
    # on, an agent that ran `git init` in /workspace/repo -- an unremarkable thing
    # to do -- makes `go build` shell out to git, and git refuses to read a tree
    # whose uid does not match the container user ("detected dubious ownership").
    # Go reports that as a build error, and the required module fails for a reason
    # having nothing to do with the port.  Nothing here grades the stamp.
    build = tc.run([tc.GO, "build", "-trimpath", "-buildvcs=false",
                    "-o", str(tc.BINARY), "."],
                   cwd=src, env=env, timeout=2700, log="build.log")
    rep.record("compiles-offline", build.returncode == 0,
               f"go build -o {tc.BINARY.name} . succeeded against the complete mirror",
               tc.output_of(build), weight=2.0, required=True)
    if build.returncode != 0 or not tc.BINARY.exists():
        # Say which kind of failure this was.  The mirror stocks no ALTERNATIVE
        # router, so a build that died on a missing .zip most likely swapped the
        # retired router for a peer -- which is not this migration, and reads very
        # differently from a syntax error.  Diagnosis only: the check above already
        # recorded the verdict.
        dl = tc.run([tc.GO, "mod", "download", "all"], cwd=src, env=env,
                    timeout=1800, log="mod-download.log")
        text = tc.output_of(build) + "\n" + tc.output_of(dl)
        blocked = sorted({m for m in tc.module_list("banned-routers.txt")
                          if m in text and m not in tc.module_list("retired-modules.txt")})
        if blocked:
            rep.note("resolve-hint",
                     f"the build asked the mirror for an archive of "
                     f"{', '.join(blocked)}, which it does not serve: this task's "
                     f"destination is the standard library, so no third-party "
                     f"router is stocked. A `require` line alone would not have "
                     f"triggered a fetch, so something imports it.")
        return rep.finish({"stage": "build"})

    # Whether the manifest is TIDY is a different question from whether the tree
    # builds, and on this stage's axis it is not a question at all: a stale
    # `require` line is untidiness, not a behavioural regression.  Recorded as an
    # observation for the same reason the retirement facts below are.
    dl = tc.run([tc.GO, "mod", "download", "all"], cwd=src, env=env,
                timeout=1800, log="mod-download.log")
    if dl.returncode != 0:
        rep.note("manifest-not-resolvable",
                 "the tree compiles, but `go mod download all` cannot resolve "
                 "every `require` line against the mirror -- a line survives "
                 "that no package imports.\n\n" + tc.output_of(dl))

    rep.record("binary-is-executable", os.access(tc.BINARY, os.X_OK),
               f"{tc.BINARY} is executable", weight=1.0, required=True)

    _record_dependency_observations(rep, env)

    # A binary that compiled but cannot start is not measurable, and the failure
    # would otherwise surface as 87 unexplained connection refusals.  `-help`
    # exits non-zero in Go's flag package by convention, so the check is on the
    # output rather than on the status.
    helped = tc.run([str(tc.BINARY), "-help"], env=env, timeout=60,
                    log="help.log")
    text = tc.output_of(helped)
    rep.record("binary-starts", bool(text.strip()),
               "the binary runs and prints its flag usage", text, weight=1.0)

    # The flags the rest of the suite launches with have to exist.  Every recorded
    # profile is a flag set; a submission that renamed one would fail every case
    # in that profile with a launch error, which reads like a broken server rather
    # than like a missing flag.
    needed = ("document_root", "addr", "enable_cors", "enable_auth",
              "read_only_tokens", "read_write_tokens", "max_upload_size")
    missing = [f for f in needed if f"-{f}" not in text]
    rep.record("profile-flags-exist", not missing,
               "every flag the recorded profiles launch with is still declared",
               f"missing: {', '.join(missing)}\n\n{text}" if missing else text,
               weight=1.0)

    return rep.finish({
        "stage": "complete",
        "binary": str(tc.BINARY),
        "binary_bytes": tc.BINARY.stat().st_size,
        "go": (tc.run([tc.GO, "version"], env=env, timeout=60).stdout or "").strip(),
    })


if __name__ == "__main__":
    sys.exit(main())
