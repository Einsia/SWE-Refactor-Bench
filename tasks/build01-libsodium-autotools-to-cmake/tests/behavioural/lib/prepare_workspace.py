#!/usr/bin/env python3
"""Prepare the collected repository for verification.

Design rule: **delete only what would block or contaminate a from-source
rebuild, record everything that was deleted, and forgive nothing.** The
audit suite reads this report and decides. Nothing is silently cleaned up
on the agent's behalf.

In particular, Autotools leftovers (configure, Makefile.in, *.am, *.ac, m4/,
build-aux/, config.status, libtool) are NOT deleted — their presence is the
thing being measured, so they must survive into the audit checks.

Categories:
  build_trees  build/ CMakeFiles/ CMakeCache.txt build.ninja …  deleted+recorded
               (a build tree inside the source dir also means the agent built
               in-source, which the contract forbids)
  binaries     *.o *.a *.so *.so.N *.lo *.la                    deleted+recorded
               (so no prebuilt library can satisfy a behavioural check)
  vendored     node_modules/ vendor/ third_party/ _deps/        deleted+recorded
  scratch      __pycache__/ .cache/ *.pyc *.res *.trs *.log     deleted+recorded
"""
import argparse
import json
import os
import shutil

BUILD_TREE_DIRS = {
    "build", "Build", "_build", "builddir", "build-cmake", "cmake-build",
    "cmake-build-debug", "cmake-build-release", "CMakeFiles", "_CPack_Packages",
    "out", "_out", "dist", "_install", "install", "_stage", "stage",
    "prefix", "_prefix", ".ninja",
}
BUILD_TREE_FILES = {
    "CMakeCache.txt", "cmake_install.cmake", "CTestTestfile.cmake",
    "install_manifest.txt", "build.ninja", ".ninja_deps", ".ninja_log",
    "rules.ninja", "compile_commands.json", "DartConfiguration.tcl",
    "CPackConfig.cmake", "CPackSourceConfig.cmake",
}
VENDORED_DIRS = {"node_modules", "vendor", "third_party", "_deps", "subprojects"}
SCRATCH_DIRS = {
    "__pycache__", ".cache", ".ccache", ".pytest_cache", ".mypy_cache",
    ".zig-cache", "zig-out", ".deps", ".libs", "Testing",
}
BINARY_SUFFIXES = (".o", ".obj", ".lo", ".la", ".a", ".so", ".dylib", ".dll")
SCRATCH_SUFFIXES = (".pyc", ".res", ".trs", ".gcda", ".gcno", ".gch", ".pch",
                    ".dSYM")
# Sources the agent is forbidden to touch: never delete, whatever else matches.
PROTECTED = lambda rel: (
    rel.startswith(("src/", "test/")) and rel.endswith((".c", ".h", ".S", ".exp"))
)


def tree_stats(path):
    files = 0
    size = 0
    for r, _d, ff in os.walk(path):
        for f in ff:
            files += 1
            try:
                size += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return files, size


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--snapshot", required=True,
                    help="where to leave the pristine copy of the delivered "
                         "sources; every build in the matrix is made from it")
    args = ap.parse_args()
    repo = os.path.abspath(args.repo)

    rec = {"build_trees": [], "binaries": [], "vendored": [], "scratch": []}

    def drop_dir(full, rel, cat):
        files, size = tree_stats(full)
        rec[cat].append({"path": rel, "kind": "dir", "files": files, "bytes": size})
        shutil.rmtree(full, ignore_errors=True)

    def drop_file(full, rel, cat):
        try:
            size = os.path.getsize(full)
        except OSError:
            size = 0
        rec[cat].append({"path": rel, "kind": "file", "bytes": size})
        try:
            os.remove(full)
        except OSError:
            pass

    # ---- directories, top-down so whole trees are pruned in one step -------
    for dirpath, dirnames, _f in os.walk(repo, topdown=True):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for d in list(dirnames):
            full = os.path.join(dirpath, d)
            rel = os.path.relpath(full, repo)
            cat = None
            if d in BUILD_TREE_DIRS:
                cat = "build_trees"
            elif d in VENDORED_DIRS:
                cat = "vendored"
            elif d in SCRATCH_DIRS or d.endswith(".dSYM"):
                cat = "scratch"
            if cat:
                drop_dir(full, rel, cat)
                dirnames.remove(d)

    # ---- files -------------------------------------------------------------
    for dirpath, dirnames, files in os.walk(repo):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for f in files:
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, repo)
            if PROTECTED(rel):
                continue
            cat = None
            if f in BUILD_TREE_FILES:
                cat = "build_trees"
            elif f.endswith(BINARY_SUFFIXES) or ".so." in f:
                cat = "binaries"
            elif f.endswith(SCRATCH_SUFFIXES):
                cat = "scratch"
            if cat:
                drop_file(full, rel, cat)

    # ---- prune directories that became empty ------------------------------
    for _ in range(8):
        for dirpath, dirnames, files in os.walk(repo, topdown=False):
            if dirpath == repo or dirnames or files or ".git" in dirpath:
                continue
            try:
                os.rmdir(dirpath)
            except OSError:
                pass

    # ---- pristine snapshot of the delivered sources ------------------------
    snap = os.path.abspath(args.snapshot)
    os.makedirs(os.path.dirname(snap) or ".", exist_ok=True)
    if os.path.exists(snap):
        shutil.rmtree(snap)
    shutil.copytree(repo, snap, symlinks=True,
                    ignore=shutil.ignore_patterns(".git"))

    report = dict(rec)
    report["repo"] = repo
    report["delivered_snapshot"] = snap
    # An in-source build is anything build-tree-ish at the repo root or nested
    # inside the source directories.
    report["in_source_build_evidence"] = [
        e for e in rec["build_trees"]
        if not e["path"].startswith(("build/", "_build/", "builddir/"))
        or e["path"].count("/") == 0
    ]
    with open(args.report, "w") as fh:
        json.dump(report, fh, indent=1)

    for cat in ("build_trees", "binaries", "vendored", "scratch"):
        n = len(rec[cat])
        if n:
            print("prepare: %-12s %d entries" % (cat, n))
            for e in rec[cat][:12]:
                print("    %s" % e["path"])


if __name__ == "__main__":
    main()
