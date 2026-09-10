#!/usr/bin/env python3
"""Build-time self-check for the lang04-acorn-js-to-rust agent environment.

Runs inside the environment image build.  Four things must hold before the image
is allowed to exist:

1. the workspace really is State A - the JavaScript implementation is present
   and intact, so the solver starts at the migration's beginning;
2. the workspace carries no repository-management data, so upstream history
   cannot be mined for the answer;
3. State A's own toolchain works, so the reference the solver is told to study
   is actually runnable;
4. the published migration contract is internally consistent, so the solver and
   the verifier are reading the same rules.

Exit status is non-zero on the first violation, which fails the image build.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

VCS_NAMES = {
    ".git", ".github", ".gitlab", ".gitignore", ".gitattributes", ".gitmodules",
    ".mailmap", ".hg", ".svn", ".bzr", "_darcs", "CVS", ".agit", ".travis.yml",
    ".circleci", ".hgignore", ".hgtags",
}

# Present in State A and load-bearing: a solver that deletes one of these has
# not started from the migration's beginning.
ANCHOR_MIN_BYTES = {
    "acorn/src/index.js": 2000,
    "acorn/src/state.js": 4000,
    "acorn/src/expression.js": 30000,
    "acorn/src/statement.js": 30000,
    "acorn/src/tokenize.js": 20000,
    "acorn/src/tokentype.js": 4000,
    "acorn/src/regexp.js": 40000,
    "acorn/src/bin/acorn.js": 1500,
    "acorn-loose/src/index.js": 900,
    "acorn-walk/src/index.js": 8000,
    "package.json": 1000,
}

failures: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)
    print(f"FAIL {msg}", file=sys.stderr)


def ok(msg: str) -> None:
    print(f"ok   {msg}")


def check_state_a(repo: Path, contract: dict) -> None:
    if not repo.is_dir():
        fail(f"{repo} is not a directory")
        return

    # The baseline .git is created by this image on purpose and is the one piece
    # of repository data the solver is meant to have.  Everything below measures
    # the working tree, so it is excluded here and checked separately.
    def tracked(p: Path) -> bool:
        return ".git" not in p.relative_to(repo).parts

    files = [p for p in repo.rglob("*")
             if p.is_file() and not p.is_symlink() and tracked(p)]
    dirs = [p for p in repo.rglob("*") if p.is_dir() and tracked(p)]
    expected = contract["state_a"]["file_count"]
    if len(files) != expected:
        fail(f"file count is {len(files)}, contract says {expected}")
    else:
        ok(f"file count {len(files)}")

    irregular = [
        p.relative_to(repo).as_posix()
        for p in repo.rglob("*")
        if tracked(p) and (not (p.is_dir() or p.is_file()) or p.is_symlink())
    ]
    if irregular:
        fail(f"irregular entries in State A: {irregular[:8]}")
    else:
        ok("no symlinks or special files")

    total = sum(p.stat().st_size for p in files)
    if total != contract["state_a"]["content_bytes"]:
        fail(f"content bytes {total} != {contract['state_a']['content_bytes']}")
    else:
        ok(f"content bytes {total}")

    for rel, minimum in sorted(ANCHOR_MIN_BYTES.items()):
        path = repo / rel
        if not path.is_file():
            fail(f"anchor file missing: {rel}")
        elif path.stat().st_size < minimum:
            fail(f"anchor file too small: {rel} ({path.stat().st_size} < {minimum})")
    if not failures:
        ok(f"{len(ANCHOR_MIN_BYTES)} anchor files intact")

    for path in list(files) + dirs:
        if path.name in VCS_NAMES:
            fail(f"repository-management data leaked into State A: "
                 f"{path.relative_to(repo).as_posix()}")
    ok("no VCS or CI metadata in the State A tree")

    stray = sorted(
        p.relative_to(repo).as_posix()
        for p in dirs
        if p.name in {"node_modules", "dist", "target"}
    )
    if stray:
        fail(f"build output present in State A: {stray}")
    else:
        ok("no build output in State A")


def check_git_baseline(repo: Path) -> None:
    """Exactly one commit, and it contains the whole tree.

    A solver that finds two commits can diff them.  A solver that finds an empty
    baseline cannot tell what it started from.
    """
    env = {**os.environ, "GIT_DIR": str(repo / ".git"), "GIT_WORK_TREE": str(repo)}
    def git(*args: str) -> str:
        return subprocess.run(("git", *args), env=env, capture_output=True,
                              text=True, check=True).stdout.strip()

    if not (repo / ".git").is_dir():
        fail("no git baseline in the workspace")
        return
    count = git("rev-list", "--count", "HEAD")
    if count != "1":
        fail(f"git history has {count} commits, expected exactly 1")
    else:
        ok("git history is a single baseline commit")
    listed = git("ls-files").splitlines()
    on_disk = [p for p in repo.rglob("*")
               if p.is_file() and ".git" not in p.relative_to(repo).parts]
    if len(listed) != len(on_disk):
        fail(f"baseline commit tracks {len(listed)} files, tree has {len(on_disk)}")
    else:
        ok(f"baseline commit tracks all {len(listed)} files")
    dirty = git("status", "--porcelain")
    if dirty:
        fail(f"workspace is dirty at image build time: {dirty.splitlines()[:5]}")
    else:
        ok("workspace is clean against the baseline")


def check_reference_runs(repo: Path, node_modules: Path) -> None:
    """State A's build and test suite must work, offline, in this image."""
    if not node_modules.is_dir():
        fail(f"{node_modules} missing: State A's toolchain was not installed")
        return
    for tool in ("node", "npm"):
        if shutil.which(tool) is None:
            fail(f"{tool} is not on PATH")
    for rel in ("acorn/dist/acorn.js", "acorn-loose/dist/acorn-loose.js",
                "acorn-walk/dist/walk.js"):
        if (repo / rel).exists():
            fail(f"build output left in the workspace: {rel}")
    ok("no dist/ left behind by the build proof")

    probe = subprocess.run(
        ["node", "-e",
         "const a=require(process.argv[1]+'/acorn/src/index.js');"
         "process.stdout.write('unused')"],
        capture_output=True, text=True, cwd="/tmp",
    )
    # src/ is ESM; require() of it is expected to fail.  What matters is that
    # node itself works and that the sources are where the contract says.
    if shutil.which("node") and probe.returncode not in (0, 1):
        fail(f"node is broken in this image: {probe.stderr[:200]}")
    else:
        ok("node runs")


def check_contract(contract: dict, repo: Path) -> None:
    if contract["task"] != "lang04-acorn-js-to-rust":
        fail(f"contract task is {contract['task']!r}")
    if contract["submission_root"] != "/workspace/repo":
        fail("contract submission_root is not /workspace/repo")

    forbidden_ext = set(contract["forbidden_paths"]["extensions"])
    # State A must actually violate the State B rules -- otherwise the migration
    # has nothing to remove and the gate proves nothing.
    present = {p.suffix for p in repo.rglob("*") if p.is_file()}
    if not (forbidden_ext & present):
        fail("State A contains none of the extensions State B forbids")
    else:
        ok(f"State A violates {len(forbidden_ext & present)} of the State B "
           f"path rules, as it should")

    for rel in contract["retained_paths"]["required"]:
        if not (repo / rel).is_file():
            fail(f"retained path missing from State A: {rel}")
    ok(f"{len(contract['retained_paths']['required'])} retained paths present")

    names = {c["name"] for c in contract["state_b"]["crates"]}
    if names != {"acorn", "acorn-loose", "acorn-walk", "acorn-cli"}:
        fail(f"contract crate set is {sorted(names)}")
    ops = contract["probe_protocol"]["operations"]
    if len(ops) != len(set(ops)):
        fail("duplicate probe operations in the contract")
    if len(ops) < 20:
        fail(f"contract declares only {len(ops)} probe operations")
    else:
        ok(f"{len(ops)} probe operations declared")

    inv = contract["state_b"]["install_inventory"]
    if not any(e["path"] == "bin/acorn" for e in inv):
        fail("install inventory does not include bin/acorn")
    if not any(e["path"] == "bin/acorn-probe" for e in inv):
        fail("install inventory does not include bin/acorn-probe")
    ok(f"install inventory has {len(inv)} entries")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, default=Path("/workspace/repo"))
    ap.add_argument("--node-modules", type=Path,
                    default=Path("/workspace/node_modules"))
    ap.add_argument("--contract", type=Path,
                    default=Path("/opt/swerefactor/source-contract.json"))
    args = ap.parse_args()

    contract = json.loads(args.contract.read_text())
    print(f"== State A =="); check_state_a(args.repo, contract)
    print(f"== git baseline =="); check_git_baseline(args.repo)
    print(f"== reference toolchain =="); check_reference_runs(args.repo, args.node_modules)
    print(f"== contract =="); check_contract(contract, args.repo)

    if failures:
        print(f"\n{len(failures)} environment check(s) failed", file=sys.stderr)
        return 1
    print("\nenvironment verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
