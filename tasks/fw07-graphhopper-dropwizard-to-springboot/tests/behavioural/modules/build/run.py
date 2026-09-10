"""The build module: build both sides, ask both, publish the ledger.

This is the only module that compiles anything or opens a socket.  It is declared
first and required in suite.toml, and it is SCORED rather than treated as setup —
a submission that does not build has failed the most basic thing the task asked
for, and that belongs on a weighted module rather than surfacing as an
infrastructure error that reads like the grader broke.

In order:

  1. Unpack State A from data/original.tar.gz into a private directory.  Stage 2
     does not get /opt/original — the schema makes that mount optional here, and a
     stage whose result depended on how it was invoked is a stage that is hard to
     trust.  The tarball is byte-identical to environment/original.tar.gz, which
     `swerefactor validate` enforces, so State A here is the State A the agent
     started from.

  2. Build both trees with the same offline command.

  3. Launch each under both graph profiles, via the same convention ladder, and
     ask every case twice.

  4. Write capture.json into $SRB_SUITE_WORK, plus a provenance record.

The scored checks here are about existence and liveness: both trees build, both
produce a launchable jar, both start under both profiles.  Nothing here compares
a response — that is the ten modules after it, one axis each, so a divergence
lands on the axis it belongs to instead of collapsing into "build failed".

The asymmetry between the two sides' failures matters.  A submission that will not
build scores zero on this module and the stage continues, reporting zeros with
reasons.  A REFERENCE that will not build means no number computed from it means
anything, so the module exits 70 — the grader-failure code — rather than reporting
the zero the submission would appear to have earned.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))) + "/lib")

import toolchain as tc                                     # noqa: E402
from harness import capture, corpus, maven, server         # noqa: E402

DATA = tc.DATA
WORK = capture.work_dir()
SUBMISSION = tc.REPO
REFERENCE = tc.REFERENCE_TREE

# Fixed ports, not ephemeral, because the config files name them and a config file
# cannot be written after the process it configures has started.  The two sides
# never run at the same time and still get different ports: a JVM that ignored
# SIGTERM and held its socket would otherwise make the next launch fail as a bind
# error attributed to the wrong side.
PORTS = {
    "reference": {"app": 18989, "admin": 18990},
    "submission": {"app": 19989, "admin": 19990},
}


def unpack_reference(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(DATA / "original.tar.gz") as tf:
        for m in tf.getmembers():
            parts = m.name.split("/", 1)      # strip the single leading component
            if len(parts) != 2 or not parts[1]:
                continue
            m.name = parts[1]
            tf.extract(m, dest, filter="data")
    # No mtime re-stamp here: build_side does it to whichever tree it is about to
    # build, so both sides get it from one place.  It was here, and being here is
    # what let the submission go without.


def profile_config(tree: Path, profile: str, ports: dict) -> Path:
    """Materialise a profile's config into a tree, with that side's ports.

    Both sides get the SAME template with different ports substituted.  A config
    difference between the sides would be a difference in the question rather than
    in the answer, so the templates live in data/ and neither tree's own
    config-example.yml is used — a submission is free to rewrite that file, and a
    grader that read it would be handing the submission the exam paper.
    """
    spec = corpus.PROFILES[profile]
    template = (DATA / "configs" / spec["config"]).read_text()
    graph = tree / "target" / f"srb-graph-{profile}"
    text = (template
            .replace("@APP_PORT@", str(ports["app"]))
            .replace("@ADMIN_PORT@", str(ports["admin"]))
            .replace("@GRAPH_DIR@", str(graph))
            .replace("@REPO_ROOT@", str(tree)))
    out = tree / f"srb-{profile}.yml"
    out.write_text(text)
    return out


# `normalise_mtimes` lives in harness/maven.py beside `scrub_build_output`, not
# here.  Stage 3 builds both trees through that copied harness, so a definition
# here would be a second copy of the re-stamp -- and two copies of a timestamp
# rule are two rules, which is what the call below and `stage3.py`, which consumes
# the count it returns, would then disagree about.
def build_side(report: tc.Report, name: str, tree: Path, weight: float,
               caveats: dict, modules: dict,
               ref_module: str | None = None) -> Path | None:
    """Build one tree and return its launchable jar, or None having said why.

    `ref_module` is the reactor module the *other* side's app jar came out of, when
    it is already known.  It turns the jar record from "a launchable jar exists"
    into "and it is, or is not, the same module's jar" — see the record below for
    why that difference is the whole finding on a submission that never repackaged.
    Anything it establishes lands in `caveats[name]` for the launch record to lead
    with, because a launch failure is read before a passing build check is.

    The reactor module list lands in `modules[name]`, which carries it into the
    capture.  Its reader was the `provenance` module, which is retired; the list is
    still written, because it is a record of what was built that a reader of the work
    directory can use, but nothing scores it now.
    """
    log = tc.WORK / f"build-{name}.log"
    # Both sides, before either is built.  On the reference this removes nothing —
    # it was unpacked from the tarball moments ago and State A ships no build
    # output — and it is still called, because the claim this module makes is that
    # the jar it grades came out of the build it just ran, and that claim has to be
    # true of both sides or it is not a claim about the comparison.  What it stops
    # on the submission side is a committed jar being discovered by find_app_jar
    # and graded in place of the sources.
    scrubbed = maven.scrub_build_output(tree)
    # Both sides, for the same reason, and the symmetry is the point: re-stamping
    # only the reference would leave the comparison between a tree whose resources
    # copied and a tree whose resources did not.
    #
    # The shared snapshot builder fixes every mtime in the delivered tree at epoch
    # 0.  maven-resources-plugin's incremental copy asks whether
    # source.lastModified() > destination.lastModified(); a destination that does
    # not exist yet answers 0, and 0 > 0 is false.  So it creates the destination
    # directories, logs "Copying 76 resources", and writes none of them.  Only the
    # *filtered* resources land, because filtering rewrites unconditionally --
    # which is why core/target/classes ends up holding `version`, `builddate` and
    # `gitinfo` and not one of the 49 translation files.
    #
    # A tree that hits this builds green, packages a jar, and dies at startup in
    # TranslationMap.doImport with "No input stream found in class path!?" -- or
    # worse, starts and answers a German route in English.  Neither failure names
    # a timestamp, and both look like the tree's own fault.
    #
    # In harness/maven.py beside scrub_build_output, not local to this module:
    # stage 3 builds both trees too, through the same copied file, and its
    # reference is unpacked from the same epoch-0 tarball.  Without the re-stamp
    # there, State A shades a jar with none of its 49 translations and fails to
    # bind a port on every rung, which reads as "does not pass on the original"
    # for every candidate in all six rounds.
    maven.normalise_mtimes(tree)
    t0 = time.monotonic()
    try:
        text = maven.build(tree, log, goals=["package"])
    except maven.BuildFailed as exc:
        interesting = [l for l in exc.log.splitlines()
                       if any(k in l for k in
                              ("ERROR", "BUILD FAILURE", "does not exist",
                               "cannot find symbol", "Could not resolve",
                               "Non-resolvable"))]
        report.record(
            f"build-{name}", False,
            f"`mvn -B -o -DskipTests package` failed for the {name}",
            detail="\n".join(interesting[:60]) or str(exc), weight=weight)
        return None
    except subprocess.TimeoutExpired:
        report.record(f"build-{name}", False,
                      f"the {name} build did not finish within its timeout",
                      detail=f"log: {log}", weight=weight)
        return None

    took = time.monotonic() - t0
    mods = maven.module_list(tree, text)
    modules[name] = mods
    report.record(f"build-{name}", True,
                  f"the {name} built offline in {took:.0f}s across "
                  f"{len(mods)} reactor module(s)",
                  detail="\n".join(mods)
                  + (f"\nremoved before building: {', '.join(scrubbed)}"
                     if scrubbed else "\nno pre-existing build output"),
                  weight=weight)

    try:
        jar, notes = maven.find_app_jar(tree)
    except RuntimeError as exc:
        report.record(f"jar-{name}", False,
                      f"the {name} build succeeded but produced no jar that "
                      f"`java -jar` could start",
                      detail=str(exc), weight=weight)
        return None
    # The entry point, and whether this is the same module's jar as the other side's.
    #
    # `find_app_jar` takes the largest launchable jar anywhere under `target/`, which
    # instruction.md documents, and that rule cannot tell an application from a tool
    # that merely has a Main-Class.  On a submission that bound Spring Boot's
    # `repackage` GOAL to a phase literally named `repackage` -- so the execution
    # never fired and `web/target/*.jar` stayed thin -- the rule fell through to
    # `tools/...-jar-with-dependencies.jar`, whose Main-Class is
    # `com.graphhopper.tools.Measurement`, a benchmarking harness.  This check
    # recorded PASS, "the submission produced a launchable jar", and every rung of
    # the launch ladder then started Measurement and reported the same
    # `IllegalArgumentException: If no graph.location is provided you need to specify
    # an OSM file` -- which reads as a config-binding defect and sent the reviewer to
    # the wrong file entirely.  The verdict stays: the rule is documented and
    # `start-submission-*` already scores the launch failure at weight 2.0, so
    # failing here too would charge one defect twice.  What changes is that the
    # record now says which class it selected and, when the module differs, that the
    # other side's module produced no launchable jar.
    mod = maven.module_dir(tree, jar)
    entry = maven.entry_point(jar) or "no Main-Class readable"
    summary = (f"the {name} produced a launchable jar: {jar.relative_to(tree)} "
               f"({jar.stat().st_size // (1024 * 1024)} MiB), which runs {entry}")
    if ref_module is not None and mod != ref_module:
        thin = [rel for rel, ok, _ in maven.module_jars(tree, ref_module) if not ok]
        caveat = (f"this jar is from `{mod}`, not `{ref_module}` -- the module the "
                  f"reference's application jar came from; the largest launchable "
                  f"jar in the tree was taken, which is the documented rule")
        if thin:
            caveat += (f", and `{ref_module}` produced {', '.join(thin)} with no "
                       f"Main-Class in the manifest")
        caveats[name] = caveat
        summary += f". NOTE: {caveat}"
    report.record(f"jar-{name}", True, summary,
                  detail="\n".join(notes), weight=weight)
    return jar


def ask_profile(report: tc.Report, name: str, tree: Path, jar: Path,
                profile: str, answers: dict, rungs: dict,
                weight: float, caveat: str = "") -> bool:
    """Launch one side under one profile and ask that profile's cases."""
    ports = PORTS[name]
    cfg = profile_config(tree, profile, ports)
    cases = [c for c in corpus.all_cases() if c["profile"] == profile]
    try:
        srv = server.launch(f"{name}-{profile}", jar, cfg, tree,
                            tc.WORK / "server-logs",
                            ports["app"], ports["admin"])
    except server.LaunchError as exc:
        # The jar caveat leads, when there is one.  Every rung in the ladder starts
        # the same jar, so if the wrong jar was selected then all six rung excerpts
        # are that jar's complaint repeated six times, and the reader has no way to
        # tell from them that the jar itself is the finding.  The detail is clipped
        # at 4000 characters, so this has to be at the front rather than appended.
        detail = f"{caveat}\n\n{exc}" if caveat else str(exc)
        report.record(
            f"start-{name}-{profile}", False,
            f"the {name} would not start under the `{profile}` profile; every "
            f"launch convention in the ladder was tried"
            + (f" -- but see the jar note in this detail and in jar-{name}"
               if caveat else ""),
            detail=detail, weight=weight)
        return False
    rungs[f"{name}-{profile}"] = srv.rung
    # What the launcher actually established, and no more.  This said "answered on
    # both ports" while `launch` probed only the app one -- so a submission whose
    # admin connector never bound was recorded as answering on it, at weight 2.0,
    # and the admin cases then failed with nothing in the report to connect them
    # to a cause.  The admin port is not required to launch (see Server), so its
    # state belongs here as an observation.
    admin_note = (f"the admin port answered ({srv.admin_detail})"
                  if srv.admin_ready else
                  f"the admin port did NOT answer ({srv.admin_detail}); cases "
                  f"that ask it will fail")
    report.record(
        f"start-{name}-{profile}", True,
        f"the {name} started under `{profile}` via the `{srv.rung}` convention "
        f"and stayed up on its app port; {admin_note}", weight=weight)
    try:
        answers.setdefault(name, {}).update(
            capture.ask_side(srv, cases, DATA, repeats=2))
    finally:
        srv.stop()
    return True


def main() -> int:
    report = tc.Report()
    tc.WORK.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)

    if not REFERENCE.exists():
        unpack_reference(REFERENCE)

    trees = {"reference": REFERENCE, "submission": SUBMISSION}
    profiles = sorted(corpus.PROFILES)

    # Weights inside this module.  The submission's build and launch carry the
    # weight; the reference's carry none, because the reference is the grader's
    # own artefact and a submission cannot affect it.  Recording them at weight
    # zero still puts them in the report, which is where a grader failure is
    # diagnosed from.
    # Sequentially, and in this order, so the submission's jar record can be made
    # against the reference's: which module the grader's own app jar came out of is
    # the only non-arbitrary answer to "was the right jar picked?", and it is not
    # known until the reference has been built.
    caveats: dict = {}
    modules: dict = {}
    jars = {}
    jars["reference"] = build_side(report, "reference", trees["reference"], 0.0,
                                   caveats, modules)
    ref_module = (maven.module_dir(trees["reference"], jars["reference"])
                  if jars["reference"] is not None else None)
    jars["submission"] = build_side(report, "submission", trees["submission"], 3.0,
                                    caveats, modules, ref_module=ref_module)
    if jars["reference"] is None:
        return tc.grader_failure(
            report, "reference-buildable",
            "the reference tree did not build, so there is nothing to compare "
            "the submission against and no score computed here would mean "
            "anything.  The submission has NOT been judged.")

    answers: dict = {}
    rungs: dict = {}
    started = {}
    for side in ("reference", "submission"):
        if jars[side] is None:
            continue
        for profile in profiles:
            started[(side, profile)] = ask_profile(
                report, side, trees[side], jars[side], profile, answers, rungs,
                weight=0.0 if side == "reference" else 2.0,
                caveat=caveats.get(side, ""))

    if not all(started.get(("reference", p)) for p in profiles):
        return tc.grader_failure(
            report, "reference-startable",
            "the reference would not start under every profile, so the corpus "
            "has no answers to compare against.  The submission has NOT been "
            "judged.")

    doc = capture.build_capture(
        answers.get("reference", {}), answers.get("submission", {}),
        corpus.all_cases(),
        meta={
            "rungs": rungs,
            "ports": PORTS,
            "profiles": {p: corpus.PROFILES[p] for p in profiles},
            "reference_tree": str(REFERENCE),
            "submission_tree": str(SUBMISSION),
            "jars": {k: (str(v) if v else None) for k, v in jars.items()},
            # Both reactor module lists.  Nothing reads them: they are here so a
            # dropped, added or renamed Maven module is a structural fact in the
            # work directory rather than something inferred from a score.
            "modules": modules,
        })
    path = capture.write(doc)

    # How many cases the submission answered at all.  Not a comparison — that is
    # every module after this one — but the difference between "answered wrongly"
    # and "did not answer" is worth having in the report, because they look
    # identical in a score and mean different things about the submission.
    answered = sum(
        1 for c in doc["cases"].values()
        if c["submission"] and not any("error" in a for a in c["submission"]))
    total = len(doc["cases"])
    # "from both sides" is only true if the submission started.  When it did not,
    # 0/88 is a restatement of the launch failure rather than a second finding,
    # and saying so here is the difference between a reader seeing one cause and
    # hunting for two.  The profiles it failed under are named, because a
    # submission can start under one and not the other.
    unstarted = [p for p in profiles if not started.get(("submission", p))]
    if unstarted:
        capture_summary = (
            f"recorded {total} case(s) from the reference; the submission did "
            f"not start under {', '.join('`' + p + '`' for p in unstarted)}, so "
            f"it answered {answered}/{total} -- see the start-submission checks "
            f"above for why, not this one")
    else:
        capture_summary = (
            f"recorded {total} case(s) from both sides; the submission answered "
            f"{answered}/{total} without a transport error")
    report.record(
        "capture", answered > 0, capture_summary,
        detail=f"ledger: {path}\ncorpus fingerprint: {doc['corpus_fingerprint']}",
        weight=1.0)

    tc.PROVENANCE.write_text(json.dumps({
        "rungs": rungs,
        "jars": {k: (str(v) if v else None) for k, v in jars.items()},
        "corpus_fingerprint": doc["corpus_fingerprint"],
        "captured_at": doc["captured_at"],
    }, indent=1))

    return report.finish({
        "corpus_fingerprint": doc["corpus_fingerprint"],
        "cases": total,
        "answered_by_submission": answered,
        "launch_rungs": rungs,
    })


if __name__ == "__main__":
    sys.exit(main())
