"""Decide every expectation this suite grades against, at image build time.

Run once, in the behavioural image's Dockerfile, before any submission exists.  It
unpacks the suite's own digest-verified copy of State A, builds it for all three
targets, runs every program under each, and writes the answers -- plus the screen,
the labels, the blob-artifact outputs and the bignum reference values -- into
/opt/assets as an immutable, digested blob.  Grading then only compares.

Why this is not done at grading time.  Building State A next to a submission means
running the submission's `make` in the same container, as root, while the answers are
being decided.  A `Makefile` is a program: it can write to another directory, replace
a compiler on PATH, or edit the answer sheet that was collected a minute earlier.
None of that requires cleverness, only a rule with a recipe.  Sealing the answers
into the image removes the whole class, and it has a second effect that is worth as
much: grading builds three trees instead of six, so the stage finishes in half the
time and a timeout stops being the thing most likely to fail an honest submission.

What is deliberately *not* frozen: anything about the submission.  This file never
sees one.  It reads a tarball whose digest is checked twice -- once by the Dockerfile
and once here -- and nothing else.

Usage:

    python3 -I lib/freeze.py --archive data/original.tar.gz \\
        --sha256 <hex> --assets /opt/assets --workspace /tmp/freeze
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import targets as T  # noqa: E402

#: Bumped when the meaning of anything in the blob changes, so a grading run that
#: meets an older blob says so instead of comparing against a different contract.
ASSET_SCHEMA = "swerefactor-pf03-frozen-v2"

#: File names inside the assets directory.
ANSWERS_DIR = "answers"
SCREEN_FILE = "screen.json"
LABELS_FILE = "labels.json"
ORACLE_FILE = "oracle.json"
MANIFEST_FILE = "manifest.json"


def _digest_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def unpack(archive, expect_sha, dest):
    """Verify the archive and unpack State A into dest, stripping the top directory.

    The digest is re-checked here even though the Dockerfile checked it, because
    this file is what decides the answers and the check belongs next to the
    decision.  A mismatch is fatal: an expectation computed from the wrong tree is
    worse than no expectation, since it produces a number that looks like a grade.
    """
    actual = _digest_file(archive)
    if expect_sha and actual != expect_sha:
        raise SystemExit("freeze: %s sha256 %s != expected %s"
                         % (archive, actual, expect_sha))
    os.makedirs(dest, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        tops = {m.name.split("/", 1)[0] for m in members}
        if len(tops) != 1:
            raise SystemExit("freeze: archive has %d top-level entries" % len(tops))
        top = tops.pop()
        for m in members:
            if m.name.startswith("/") or ".." in m.name.split("/"):
                raise SystemExit("freeze: refusing member %r" % m.name)
            m.name = m.name[len(top):].lstrip("/")
            if not m.name:
                continue
            tar.extract(m, dest, set_attrs=False)
    for probe in ("quickjs.c", "cutils.h", "Makefile", "VERSION"):
        if not os.path.isfile(os.path.join(dest, probe)):
            raise SystemExit("freeze: unpacked tree has no %s" % probe)
    return actual


def build_all(source, targets, log):
    """Build State A for every target from its own copy.  Fatal on failure."""
    builds = {}
    for target in targets:
        build = T.Build("original", target,
                        root=os.path.join(T.BUILD_ROOT, "frozen-%s" % target))
        os.makedirs(os.path.dirname(build.root), exist_ok=True)
        if os.path.exists(build.root):
            shutil.rmtree(build.root)
        shutil.copytree(source, build.root, symlinks=True)
        started = time.time()
        build.steps["make"] = T.run("make", build.make_argv(), build.root,
                                    T.BUILD_TIMEOUT)
        step = build.steps["make"]
        log("build %-8s %s in %.0fs" % (target, "ok" if step.ok else
                                        "FAILED (rc=%d)" % step.rc,
                                        time.time() - started))
        if not step.ok:
            sys.stderr.write(step.tail(60) + "\n")
            raise SystemExit("freeze: State A does not build for %s" % target)
        missing = [g for g in build.goals if not os.path.exists(build.path(g))]
        if missing:
            raise SystemExit("freeze: %s built without %s"
                             % (target, ", ".join(missing)))
        builds[target] = build
    return builds


def oracle_expectations(builds, log):
    """Everything the graded rows need from State A that is not an answer sheet.

    Three groups, each computed on the target that will be compared against:

      blob      what every blob-carrying artifact prints on x86-64;
      bignum    what the twenty bignum constants read back as, through a blob
                written by the host compiler and read by the host interpreter;
      upstream  how each of upstream's own suites behaves in State A on each
                target -- the baseline `upstream-suites` grades against, so that a
                test which fails here for the image's reasons is not charged to a
                submission.

    And two baselines of the same kind as `upstream`, for the two rows that launch
    something rather than read a sheet:

      blob_baseline    per target, which blob-carrying artifacts State A itself ran
                       to the x86-64 value;
      bignum_baseline  per target, whether State A completed the round trip and
                       which constants came back unchanged.

    Both exist for the reason `upstream` does, one step further: on s390x State A
    fails four of the five artifacts and the whole round trip, because writing a blob
    on a little-endian host and reading it on a big-endian target is the capability
    this task exists to add.  A cell State A cannot carry is the port itself, and
    charging a submission for it means the reference tree cannot pass the stage built
    out of its own answers.  Grading reads these and scores only what State A
    carried; see `RECORDED_ONLY` in driver.py.

    Nothing here is a pass list.  Each cell is measured on the tree being sealed, so
    a cell returns to the scored pool the moment State A can carry it.
    """
    import driver as D  # imported late: driver imports targets, which is loaded

    oracle = {"blob": {}, "bignum": {}, "upstream": {},
              "blob_baseline": {}, "bignum_baseline": {}}
    ref = builds[T.ORACLE_TARGET]

    for spec in D.BLOB_ARTIFACTS:
        step = D._run_blob_artifact(ref, spec)
        if step is None or not step.ok:
            raise SystemExit("freeze: State A cannot run blob artifact %s%s"
                             % (spec[0], "" if step is None
                                else " (rc=%d)\n%s" % (step.rc, step.tail(20))))
        oracle["blob"][spec[0]] = D.blob_projection(step.out)
    log("blob expectations: %d artifacts on %s"
        % (len(oracle["blob"]), T.ORACLE_TARGET))

    values, step = D._bignum_roundtrip(ref, (), D.BIGNUM_CONSTANTS, "freeze")
    if values is None:
        raise SystemExit("freeze: State A cannot complete the bignum round trip\n%s"
                         % step.tail(30))
    missing = [k for k, _e, _d in D.BIGNUM_CONSTANTS if k not in values]
    if missing:
        raise SystemExit("freeze: bignum round trip printed nothing for %s"
                         % ", ".join(missing))
    oracle["bignum"] = {k: v for k, v in values.items()
                        if not k.startswith("__")}
    log("bignum expectations: %d constants" % len(oracle["bignum"]))

    #: Which of those two rows' cells State A carries, per target, measured the same
    #: way the graded run will measure them.
    for target, build in sorted(builds.items()):
        cells = {}
        for spec in D.BLOB_ARTIFACTS:
            step = D._run_blob_artifact(build, spec)
            cells[spec[0]] = bool(
                step is not None and step.ok
                and D.blob_projection(step.out) == oracle["blob"][spec[0]])
        oracle["blob_baseline"][target] = cells
        log("blob baseline     %-8s %d of %d artifacts carried in State A"
            % (target, sum(cells.values()), len(cells)))

    for target, build in sorted(builds.items()):
        run = D.attempt_bignum_roundtrip(build, oracle["bignum"], target)
        got = run["values"] or {}
        cells = {k: bool(got.get(k) == v) for k, v in oracle["bignum"].items()}
        cells[D.ROUNDTRIP_CELL] = run["values"] is not None
        oracle["bignum_baseline"][target] = cells
        log("bignum baseline   %-8s round trip %s, %d of %d constants carried"
            % (target, "ok" if cells[D.ROUNDTRIP_CELL] else "FAILED",
               sum(1 for k, v in cells.items()
                   if v and k != D.ROUNDTRIP_CELL), len(oracle["bignum"])))

    #: The oracle target is where both expectations came from, so it has to carry
    #: every cell.  If it does not, the shared attempt path disagrees with the
    #: procedure that produced the expectations, and every other target's baseline is
    #: measured against a moving reference.
    for kind in ("blob", "bignum"):
        cells = oracle["%s_baseline" % kind][T.ORACLE_TARGET]
        short = sorted(k for k, ok in cells.items() if not ok)
        if short:
            raise SystemExit(
                "freeze: State A on %s does not carry its own %s expectations: %s. "
                "The baseline and the expectation were measured by different code "
                "paths." % (T.ORACLE_TARGET, kind, ", ".join(short)))

    for target, build in sorted(builds.items()):
        for rel in D.UPSTREAM_SUITES:
            step = D._run_upstream(build, rel)
            if step is None:
                raise SystemExit("freeze: State A has no %s" % rel)
            oracle["upstream"].setdefault(target, {})[rel] = {
                "rc": step.rc, "timed_out": step.timed_out,
                "tail": step.tail(20)}
        passing = [rel for rel, r in oracle["upstream"][target].items()
                   if r["rc"] == 0]
        log("upstream baseline %-8s %d of %d pass in State A"
            % (target, len(passing), len(D.UPSTREAM_SUITES)))
    return oracle


def write_assets(assets, sheets, screen, labels, oracle, provenance, log):
    """Lay the blob out and write a manifest that digests every file in it."""
    if os.path.exists(assets):
        shutil.rmtree(assets)
    os.makedirs(os.path.join(assets, ANSWERS_DIR))

    files = []

    def put(rel, payload):
        path = os.path.join(assets, rel)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
        files.append(rel)

    for target, sheet in sorted(sheets.items()):
        put(os.path.join(ANSWERS_DIR, "original-%s.json" % target), sheet)
    put(SCREEN_FILE, screen)
    put(LABELS_FILE, labels)
    put(ORACLE_FILE, oracle)

    counts = {
        "admitted": sum(len(v) for v in screen["admitted"].values()),
        "native": sum(len(v) for v in screen["native"].values()),
        "excluded": sum(len(v) for v in screen["excluded"].values()),
        "missing": sum(len(v) for v in screen["missing"].values()),
    }
    manifest = {
        "schema": ASSET_SCHEMA,
        "frozen_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provenance": provenance,
        "targets": list(sorted(sheets)),
        "programs": sorted(screen["admitted"]) or sorted(screen["native"]),
        "counts": counts,
        "per_program": {
            stem: {cls: len(screen[cls].get(stem) or {})
                   for cls in ("admitted", "native", "excluded", "missing")}
            for stem in sorted(set().union(*(set(screen[c]) for c in
                                             ("admitted", "native", "excluded",
                                              "missing"))))
        },
        "oracle": {"blob": sorted(oracle["blob"]),
                   "bignum": sorted(oracle["bignum"]),
                   "upstream_targets": sorted(oracle["upstream"]),
                   #: How many cells of each launching row State A carried, per
                   #: target.  In the manifest so the split is legible without
                   #: opening oracle.json, and so the Dockerfile can assert it.
                   "carried": {
                       kind: {target: sum(1 for k, ok in cells.items() if ok)
                              for target, cells in
                              sorted((oracle["%s_baseline" % kind] or {}).items())}
                       for kind in ("blob", "bignum")},
                   "cells": {
                       kind: {target: len(cells) for target, cells in
                              sorted((oracle["%s_baseline" % kind] or {}).items())}
                       for kind in ("blob", "bignum")}},
        "files": {rel: _digest_file(os.path.join(assets, rel))
                  for rel in sorted(files)},
    }
    with open(os.path.join(assets, MANIFEST_FILE), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    log("assets: %d files, %d admitted keys, %d native, %d excluded, %d missing"
        % (len(files), counts["admitted"], counts["native"], counts["excluded"],
           counts["missing"]))
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archive", required=True,
                        help="the suite's own copy of State A")
    parser.add_argument("--sha256", default="",
                        help="expected digest of the archive")
    parser.add_argument("--assets", default="/opt/assets",
                        help="where the frozen blob is written")
    parser.add_argument("--workspace", default="/tmp/freeze",
                        help="scratch for the unpacked tree and the builds")
    args = parser.parse_args(argv)

    def log(text):
        sys.stdout.write("[freeze] %s\n" % text)
        sys.stdout.flush()

    source = os.path.join(args.workspace, "state-a")
    if os.path.exists(args.workspace):
        shutil.rmtree(args.workspace)
    os.makedirs(args.workspace)
    digest = unpack(args.archive, args.sha256, source)
    log("State A unpacked from %s (sha256 %s)"
        % (os.path.basename(args.archive), digest[:16]))

    builds = build_all(source, T.TARGET_NAMES, log)

    sheets = {}
    for target, build in sorted(builds.items()):
        started = time.time()
        sheets[target] = T.collect_answers(build)
        keys = sum(len([k for k in (p.get("answers") or {})
                        if not k.startswith("__")])
                   for p in sheets[target]["programs"].values())
        failed = sorted(s for s, p in sheets[target]["programs"].items()
                        if p["rc"] != 0)
        log("answers  %-8s %4d keys in %.0fs%s"
            % (target, keys, time.time() - started,
               "" if not failed else "  (nonzero exit: %s)" % ",".join(failed)))
        if failed:
            raise SystemExit("freeze: State A's own programs failed on %s: %s"
                             % (target, ", ".join(failed)))

    screen = T.screen(sheets[T.ORACLE_TARGET], sheets[T.CONTROL_TARGET])
    labels = {"original": T.classify(sheets)}
    oracle = oracle_expectations(builds, log)

    provenance = {
        "archive": os.path.basename(args.archive),
        "archive_sha256": digest,
        "make_goals": list(T.MAKE_GOALS),
        "cross_goals": list(T.CROSS_GOALS),
        "targets": {t: {"cross_prefix": T.TARGETS[t].cross_prefix,
                        "big_endian": T.TARGETS[t].big_endian,
                        "word_bits": T.TARGETS[t].word_bits}
                    for t in T.TARGET_NAMES},
    }
    manifest = write_assets(args.assets, sheets, screen, labels, oracle,
                            provenance, log)

    #: The floors this suite's design depends on.  Checked here so a program set
    #: that has drifted fails the image build rather than grading a submission
    #: against three keys and calling it a rate.
    admitted = manifest["counts"]["admitted"]
    if admitted < 300:
        raise SystemExit("freeze: only %d admitted keys; the suite cannot grade "
                         "on that" % admitted)
    if not manifest["counts"]["native"]:
        raise SystemExit("freeze: no native keys; native-order has nothing to do")
    shutil.rmtree(args.workspace, ignore_errors=True)
    shutil.rmtree(T.BUILD_ROOT, ignore_errors=True)
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
