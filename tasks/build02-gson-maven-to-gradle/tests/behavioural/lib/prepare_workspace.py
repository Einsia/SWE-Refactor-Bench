#!/usr/bin/env python3
"""Prepare the collected repository so that what this stage builds is source.

Design rule: **delete only what would let a prebuilt artifact stand in for a
build, record everything that was deleted, and forgive nothing.**  The report is
read by the `build` module, which fails a check on anything in the `suspicious`
category.  Nothing is quietly tidied up on the submission's behalf.

What is protected: every path in State A's inventory, exactly, rather than by
rule.  If a file shipped in State A it is source and never build output, so it
survives even when its name would otherwise match a cleanup pattern.
`metrics/src/main/resources/ParseBenchmarkData.zip` is the case that matters -- a
checked-in archive, in a resource directory, which a rule about archives would
delete and a build would then fail without.

The categories, and why the difference is the whole point:

  expected     output a correct build legitimately leaves behind: `build/`,
               `target/`, `.gradle/`, and the wrapper jar a `gradle wrapper`
               invocation checks in. Deleted so this stage builds from source,
               and *not* held against the submission.
  suspicious   compiled output anywhere else, or a jar that is not the wrapper: a
               `.class` under src/, a `prebuilt/gson.jar`, a vendored dependency
               copy. Deleted too, and reported -- the `build` module fails on any
               of them, because a jar in the tree is the one thing that could make
               every parity check in this suite pass without a build.
  vendored     a dependency tree carried in the submission.
  scratch      caches and logs, which say nothing either way.

Leftovers from the retired build system -- a surviving `pom.xml`, an `.mvn`
directory -- are deliberately NOT deleted.  Their presence is measured, by the
stage-1 scan against State A and by the reviewer who can open them, so erasing
them here would destroy the evidence.
"""
import argparse
import hashlib
import json
import os
import shutil

# Directory names that are build output wherever they appear. `buildSrc` and
# `build-logic` are absent on purpose: in Gradle those are *source* -- a
# convention plugin lives there -- and deleting one would make the project
# unbuildable and then blame the submission for it. `gradle` is absent for the
# same reason: it holds the wrapper properties and the version catalog.
OUTPUT_DIRS = {"build", "target", "out", "classes", "bin", "dist",
               "generated-sources", "generated-resources", "apidocs", "javadoc"}
VENDORED_DIRS = {"node_modules", "vendor", "third_party", "_deps",
                 "m2", ".m2", "repository", "maven-repo", "offline-repo",
                 "libs-offline", "local-repo"}
SCRATCH_DIRS = {"__pycache__", ".cache", ".pytest_cache", ".mypy_cache",
                ".gradle", ".idea", ".settings", ".vscode"}

COMPILED_SUFFIXES = (".class", ".jar", ".war", ".ear", ".jmod", ".so", ".o",
                     ".a", ".dll", ".dylib", ".aar", ".apk")
SCRATCH_SUFFIXES = (".pyc", ".log", ".tmp", ".orig", ".rej", ".swp", ".bak")

# A `gradle wrapper` invocation checks these in, and a submission that ran it has
# followed ordinary Gradle practice. This stage invokes `gradle` from the image
# rather than `./gradlew`, so they are removed as output and not counted against
# anything.
WRAPPER_FILES = {"gradle/wrapper/gradle-wrapper.jar"}


def load_inventory(path):
    """State A's file list. Read from whichever key the frozen file uses."""
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    for key in ("state_a_inventory", "inventory", "files"):
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return sorted(value)
    return []


def tree_stats(path):
    files = size = 0
    for r, _d, ff in os.walk(path):
        for f in ff:
            files += 1
            try:
                size += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return files, size


def sha256(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--snapshot", default="/tmp/srb-delivered")
    ap.add_argument("--baseline", default=None,
                    help="the frozen State A inventory; never deleted")
    args = ap.parse_args()
    repo = os.path.abspath(args.repo)
    protected = set(load_inventory(args.baseline))

    rec = {"expected": [], "suspicious": [], "vendored": [], "scratch": []}

    def drop_dir(full, rel, cat, why):
        files, size = tree_stats(full)
        rec[cat].append({"path": rel, "kind": "dir", "files": files,
                         "bytes": size, "why": why})
        shutil.rmtree(full, ignore_errors=True)

    def drop_file(full, rel, cat, why):
        try:
            size = os.path.getsize(full)
        except OSError:
            size = 0
        entry = {"path": rel, "kind": "file", "bytes": size, "why": why}
        if cat == "suspicious":
            entry["sha256"] = sha256(full)
        rec[cat].append(entry)
        try:
            os.remove(full)
        except OSError:
            pass

    # ---- directories, top-down so a whole tree goes in one step -------------
    for dirpath, dirnames, _f in os.walk(repo, topdown=True):
        for d in list(dirnames):
            full = os.path.join(dirpath, d)
            rel = os.path.relpath(full, repo).replace(os.sep, "/")
            # A directory that shipped content in State A is never output.
            if any(p == rel or p.startswith(rel + "/") for p in protected):
                continue
            cat = why = None
            if d in OUTPUT_DIRS:
                cat, why = "expected", "build output directory"
            elif d in VENDORED_DIRS:
                cat, why = "vendored", "vendored dependency tree"
            elif d in SCRATCH_DIRS:
                cat, why = "scratch", "scratch directory"
            if cat:
                drop_dir(full, rel, cat, why)
                dirnames.remove(d)

    # ---- files --------------------------------------------------------------
    for dirpath, _dirnames, files in os.walk(repo):
        for f in files:
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, repo).replace(os.sep, "/")
            if rel in protected:
                continue
            cat = why = None
            if rel in WRAPPER_FILES:
                cat, why = "expected", "the Gradle wrapper's own jar"
            elif f.endswith(".class"):
                cat, why = "suspicious", "compiled class in the delivered tree"
            elif f.endswith(COMPILED_SUFFIXES):
                cat, why = "suspicious", "compiled output in the delivered tree"
            elif f.endswith(SCRATCH_SUFFIXES):
                cat, why = "scratch", "run-time scratch file"
            if cat:
                drop_file(full, rel, cat, why)

    # ---- prune directories that became empty -------------------------------
    for _ in range(8):
        for dirpath, dirnames, files in os.walk(repo, topdown=False):
            if dirpath == repo or dirnames or files:
                continue
            try:
                os.rmdir(dirpath)
            except OSError:
                pass

    # ---- pristine snapshot of the delivered sources ------------------------
    # Taken *after* cleaning and *before* any build in this stage, so every
    # configuration starts from the same known tree and a configuration that
    # corrupts its copy cannot affect the next one.
    snap = args.snapshot
    if os.path.exists(snap):
        shutil.rmtree(snap)
    shutil.copytree(repo, snap, symlinks=True,
                    ignore=shutil.ignore_patterns(".git"))

    report = dict(rec)
    report["repo"] = repo
    report["delivered_snapshot"] = snap
    report["protected_count"] = len(protected)
    report["delivered_files"] = tree_stats(snap)[0]
    with open(args.report, "w") as fh:
        json.dump(report, fh, indent=1, sort_keys=True)

    for cat in ("expected", "suspicious", "vendored", "scratch"):
        n = len(rec[cat])
        if n:
            print("prepare: %-11s %d entries" % (cat, n))
            for e in rec[cat][:12]:
                print("    %s  (%s)" % (e["path"], e["why"]))
            if n > 12:
                print("    ... and %d more" % (n - 12))
    print("prepare: %d files snapshotted to %s" % (report["delivered_files"],
                                                   snap))


if __name__ == "__main__":
    main()
