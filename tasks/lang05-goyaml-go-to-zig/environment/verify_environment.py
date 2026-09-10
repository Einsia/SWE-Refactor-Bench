#!/usr/bin/env python3
"""Assert that the lang05 agent image is what the task claims it is.

Runs as the last step of the environment build, so a broken premise stops the
image from existing rather than surfacing as a mysteriously unsolvable task.
Every check here is something that has been wrong at least once in some task's
history, or that would silently change what the task measures:

  * State A is present, complete, and free of anything that leaks the answer or
    the upstream history.
  * Both toolchains are the pinned versions.
  * Zig builds offline, with an empty package cache, and writes nothing into the
    repository.
  * The stub probe builds, speaks on stdout, and exits 70.
  * The contract's own claims about State A hold in the extracted tree, not just
    in the archive it was generated from.

Exit 0 and print a summary, or exit 1 having printed every problem found.  It
does not stop at the first: a build that fails twice for two reasons should
report both.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ZIG_VERSION = "0.14.1"
GO_VERSION = "go1.23.4"

# Anything that would hand the solver the history, the answer, or a build
# artefact it did not create.
#
# .git is not here.  The image creates one deliberately -- a single baseline
# commit, so the solver can diff and commit their work -- and forbidding it
# outright would just mean exempting it.  check_baseline_git() instead asserts the
# .git present is that one: one commit, no remotes, no upstream history to mine.
# That is the check worth having; a name test would pass on a full clone.
FORBIDDEN_NAMES = {
    ".github", ".gitlab", ".gitignore", ".gitattributes", ".gitmodules",
    ".mailmap", ".hg", ".svn", ".bzr", "_darcs", "CVS", ".agit", ".travis.yml",
    "zig-out", ".zig-cache", "zig-cache", "vendor",
}
FORBIDDEN_SUFFIXES = (".pyc", ".orig", ".rej", ".o", ".a", ".so", ".zip")

problems: list[str] = []
notes: list[str] = []


def bad(msg: str) -> None:
    problems.append(msg)


def ok(msg: str) -> None:
    notes.append(msg)


def run(cmd, cwd=None, env=None, stdin=None):
    """Run cmd, returning (rc, stdout, stderr).  Never raises: a missing binary
    is a problem to report, not a traceback to read."""
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, input=stdin,
                           capture_output=True, text=True, timeout=900)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "not found: %s" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "", "timed out: %s" % " ".join(cmd)


# ---------------------------------------------------------------------------
# State A's tree
# ---------------------------------------------------------------------------
def check_tree(repo: str, contract: dict) -> None:
    if not os.path.isdir(repo):
        bad("repo directory %s does not exist" % repo)
        return

    files, links, others = [], [], []
    for root, dirnames, filenames in os.walk(repo):
        # The baseline .git is expected; its contents are not part of the tree the
        # contract counts, and check_baseline_git() is what vouches for it.  Pruned
        # rather than skipped so the file count and content_bytes stay comparable
        # to the archive they came from.
        if ".git" in dirnames and root == repo:
            dirnames.remove(".git")
        for d in list(dirnames):
            if d in FORBIDDEN_NAMES:
                bad("forbidden directory in State A: %s" %
                    os.path.relpath(os.path.join(root, d), repo))
                dirnames.remove(d)
        for name in filenames:
            p = os.path.join(root, name)
            rel = os.path.relpath(p, repo)
            if os.path.islink(p):
                links.append(rel)
            elif not os.path.isfile(p):
                others.append(rel)
            else:
                files.append(rel)
            if name in FORBIDDEN_NAMES or name.endswith(FORBIDDEN_SUFFIXES):
                bad("forbidden file in State A: %s" % rel)

    # Symlinks would let a submission point at something outside the tree, and
    # they do not survive Harbor's artifact collection in a predictable way.
    for rel in links:
        bad("symlink in State A: %s" % rel)
    for rel in others:
        bad("non-regular file in State A: %s" % rel)

    sa = contract["state_a"]
    if len(files) != sa["file_count"]:
        bad("State A has %d files, contract says %d" % (len(files), sa["file_count"]))
    else:
        ok("%d files, as the contract declares" % len(files))

    total = sum(os.path.getsize(os.path.join(repo, f)) for f in files)
    if total != sa["content_bytes"]:
        bad("State A is %d bytes, contract says %d" % (total, sa["content_bytes"]))

    present = set(files)
    for f in sa["anchor_files"] + sa["graded_sources"]:
        if f not in present:
            bad("declared Go source missing from State A: %s" % f)
    for f in contract["retained_paths"]["required"]:
        if f not in present:
            bad("retained path missing from State A: %s" % f)

    # The build entry points the solver is told to keep.
    for f in ("build.zig", "build.zig.zon", os.path.join("src", "main.zig")):
        if f not in present:
            bad("State A is missing the Zig stub %s" % f)

    # State A must itself fail the migration gate.  If it did not, the gate
    # would pass on an unmigrated tree and would be measuring nothing.
    exts = tuple(contract["forbidden_paths"]["extensions"])
    if not any(f.endswith(exts) for f in present):
        bad("no file in State A matches forbidden_paths: the migration gate "
            "would pass on the unmigrated tree")
    else:
        ok("State A fails the migration gate, as it must")

    # .dependencies must be empty in the stub, because the instruction says the
    # solver may not add any and the stub is what they start from.
    zon = os.path.join(repo, "build.zig.zon")
    if os.path.isfile(zon):
        text = open(zon, encoding="utf-8").read()
        if not re.search(r"\.dependencies\s*=\s*\.\{\s*\}", text):
            bad("build.zig.zon does not declare an empty .dependencies")


# ---------------------------------------------------------------------------
# Toolchains
# ---------------------------------------------------------------------------
def check_toolchains() -> None:
    rc, out, err = run(["zig", "version"])
    if rc != 0:
        bad("zig is not runnable: %s" % (err or rc))
    elif out.strip() != ZIG_VERSION:
        bad("zig is %r, expected %r" % (out.strip(), ZIG_VERSION))
    else:
        ok("zig %s" % ZIG_VERSION)

    rc, out, err = run(["go", "version"])
    if rc != 0:
        bad("go is not runnable: %s" % (err or rc))
    else:
        parts = out.split()
        if len(parts) < 3 or parts[2] != GO_VERSION:
            bad("go is %r, expected %s" % (out.strip(), GO_VERSION))
        else:
            ok("%s, for studying State A" % GO_VERSION)

    # A login shell re-reads /etc/profile and can drop the image's PATH.  The
    # solver's tooling may start either kind of shell, so both have to work.
    rc, out, _ = run(["bash", "-lc", "zig version && go version"])
    if rc != 0:
        bad("a login shell cannot find both toolchains")
    else:
        ok("login shells see both toolchains")

    # The caches must be outside the repository.  This is the setting that keeps
    # build artefacts out of the collected submission.
    for var in ("ZIG_GLOBAL_CACHE_DIR", "ZIG_LOCAL_CACHE_DIR"):
        val = os.environ.get(var, "")
        if not val:
            bad("%s is not set" % var)
        elif val.startswith("/workspace"):
            bad("%s points inside /workspace (%s)" % (var, val))


# ---------------------------------------------------------------------------
# State A builds, and its own tests pass
# ---------------------------------------------------------------------------
def _tree_digest(repo: str) -> str:
    """One digest over every tracked path and its contents, so any mutation shows
    up.  .git is excluded: git refreshes its own index on read, so including it
    would make the digest differ for reasons that have nothing to do with the
    source tree, which is what this is asserting does not move."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(repo):
        if ".git" in dirnames and dirpath == repo:
            dirnames.remove(".git")
        dirnames[:] = sorted(dirnames)
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            h.update(os.path.relpath(full, repo).encode("utf-8") + b"\0")
            with open(full, "rb") as f:
                h.update(hashlib.sha256(f.read()).digest())
    return h.hexdigest()


def check_state_a_builds(repo: str) -> None:
    # GOFLAGS unset, so Go uses its default -mod=readonly.  That is the mode the
    # solver gets, and the mode under which a go command that wants to edit go.mod
    # or go.sum fails instead of doing it.  Setting -mod=mod here would hide
    # exactly the failure this function exists to rule out.
    env = dict(os.environ)
    env.pop("GOFLAGS", None)
    env["GOPROXY"] = "off"

    if not os.path.exists(os.path.join(repo, "go.sum")):
        bad("go.sum is missing from State A; in read-only mode go build fails, "
            "and with -mod=mod it would be generated into the submission tree")

    before = _tree_digest(repo)

    rc, out, err = run(["go", "build", "./..."], cwd=repo, env=env)
    if rc != 0:
        bad("State A does not build with go: %s" % (err or out)[:400])
        return
    ok("go build ./... succeeds offline in read-only module mode")

    # The solver has no network, and `go test` needs check.v1; if the module cache
    # were not primed this is where that shows up, rather than as a solver
    # reporting that the reference suite does not run.
    rc, out, err = run(["go", "test", "./..."], cwd=repo, env=env)
    if rc != 0:
        bad("State A's own tests do not pass offline: %s" % (out or err)[-600:])
    else:
        ok("go test ./... passes with GOPROXY=off")

    # Building and testing must leave no trace.  instruction.md invites the solver
    # to run the Go suite; if doing so wrote anything into the tree -- a go.sum, a
    # stray binary -- it would reach the grader as part of their submission and
    # cost them the migration gate for something the toolchain did.
    after = _tree_digest(repo)
    if after != before:
        bad("go build/test modified /workspace/repo; the tree the solver "
            "submits is not stable under the commands the task invites")
    else:
        ok("go build and go test leave the tree byte-identical")


def check_zig_builds(repo: str) -> None:
    """zig build must work offline, write nothing into the tree, and produce a
    stub that answers on stdout and exits 70.

    Done in a copy, not in place: this runs before the baseline commit in some
    build orders, and a stray zig-out/ would change the file count that the
    contract check above asserts.
    """
    tmp = tempfile.mkdtemp(prefix="l5-zigproof-")
    try:
        work = os.path.join(tmp, "repo")
        shutil.copytree(repo, work, symlinks=True)

        env = dict(os.environ)
        env["ZIG_GLOBAL_CACHE_DIR"] = os.path.join(tmp, "gcache")
        env["ZIG_LOCAL_CACHE_DIR"] = os.path.join(tmp, "lcache")

        before = {os.path.relpath(os.path.join(r, f), work)
                  for r, _, fs in os.walk(work) for f in fs}

        rc, out, err = run(["zig", "build"], cwd=work, env=env)
        if rc != 0:
            bad("zig build fails on State A: %s" % (err or out)[:400])
            return
        probe = os.path.join(work, "zig-out", "bin", "yaml-probe")
        if not os.path.isfile(probe) or not os.access(probe, os.X_OK):
            bad("zig build did not produce an executable zig-out/bin/yaml-probe")
            return
        ok("zig build produces zig-out/bin/yaml-probe")

        rc, out, err = run([probe], stdin='{"id":1,"op":"hello","source":""}\n')
        if rc != 70:
            bad("the stub probe exited %d, expected 70" % rc)
        else:
            ok("the stub probe exits 70")
        if "not implemented" not in (out + err):
            bad("the stub probe does not say it is not implemented")

        # The stub must drain stdin.  A probe that exits without reading gives
        # the grader a broken pipe; one that blocks forever costs a timeout
        # instead of a clean zero.  Feeding it more than a pipe buffer is what
        # tells those two apart.
        rc, _, _ = run([probe], stdin='{"id":1,"op":"hello","source":""}\n' * 20000)
        if rc != 70:
            bad("the stub probe exited %d on a large stdin, expected 70" % rc)
        else:
            ok("the stub probe drains a large stdin")

        rc, _, err = run(["zig", "build", "test"], cwd=work, env=env)
        if rc != 0:
            bad("`zig build test` is not a working step: %s" % err[:300])
        else:
            ok("`zig build test` runs")

        # Nothing outside the documented build-output directories.
        after = {os.path.relpath(os.path.join(r, f), work)
                 for r, _, fs in os.walk(work) for f in fs}
        allowed = ("zig-out/", ".zig-cache/", "zig-cache/")
        leaked = sorted(f for f in after - before
                        if not f.startswith(allowed))
        if leaked:
            bad("zig build wrote outside its output dirs: %s" % leaked[:8])
        else:
            ok("zig build writes only into zig-out/ and the cache")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_baseline_git(repo: str) -> None:
    """The .git in State A must be the baseline the image made, not upstream's.

    A solver who can read go-yaml's real history can read its commit messages and,
    more to the point, can `git log` their way to how every behaviour got there.
    That is not the task.  Checking the *name* .git is absent would not catch this
    -- upstream history in a directory called .git passes a name test just as
    easily as an empty one -- so this checks the shape instead: exactly one commit,
    no remotes, nothing stashed, and a clean status.
    """
    git = os.path.join(repo, ".git")
    if not os.path.isdir(git):
        bad("State A has no baseline .git; the solver cannot diff or commit")
        return

    # -c safe.directory: this runs at build time as root, before /workspace is
    # chowned to the solver, and re-runs afterwards would hit git's dubious
    # ownership guard.  Without the override that guard reports as "no HEAD",
    # which is a diagnosis of the wrong problem.
    def g(*argv):
        return run(["git", "-c", "safe.directory=" + os.path.abspath(repo),
                    *argv], cwd=repo)

    rc, out, err = g("rev-list", "--count", "HEAD")
    if rc != 0:
        bad("State A's .git has no usable HEAD: %s" % (err or out).strip()[:200])
        return
    n = out.strip()
    if n != "1":
        bad("State A's git history has %s commits, expected 1; upstream history "
            "would let the solver read the answers out of the log" % n)
    else:
        ok("git history is one baseline commit")

    rc, out, _ = g("remote")
    if rc == 0 and out.strip():
        bad("State A has git remotes configured: %s" % out.split())

    rc, out, _ = g("status", "--porcelain")
    if rc == 0 and out.strip():
        bad("State A's baseline commit does not match the tree: %s"
            % out.strip().splitlines()[:5])

    # Any other ref, note or stash would be another place to hide history.
    rc, out, _ = g("for-each-ref", "--format=%(refname)")
    if rc == 0:
        refs = sorted(r for r in out.split() if r != "refs/heads/main")
        if refs:
            bad("State A has refs beyond refs/heads/main: %s" % refs[:5])


def check_no_network() -> None:
    """The task is graded with no network, and the solver builds without one.
    This does not assert the network is *down* now -- the image build needs it --
    only that Zig is not configured to fetch anything, which is what would make
    an offline build fail.
    """
    rc, out, _ = run(["zig", "env"])
    if rc != 0:
        bad("`zig env` does not run")
        return
    try:
        info = json.loads(out)
    except ValueError:
        # 0.14 prints JSON; a future version that does not is worth noticing but
        # is not itself a broken premise.
        ok("zig env is not JSON on this version; skipped the cache-path check")
        return
    for key in ("global_cache_dir", "cache_dir"):
        val = info.get(key) or ""
        if val.startswith("/workspace"):
            bad("zig env %s points inside /workspace: %s" % (key, val))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--contract", required=True)
    args = ap.parse_args()

    try:
        with open(args.contract, encoding="utf-8") as f:
            contract = json.load(f)
    except (OSError, ValueError) as e:
        print("verify_environment: cannot read the contract: %s" % e, file=sys.stderr)
        return 1

    if contract.get("task") != "lang05-goyaml-go-to-zig":
        bad("contract is for %r, not lang05" % contract.get("task"))

    check_tree(args.repo, contract)
    check_baseline_git(args.repo)
    check_toolchains()
    check_no_network()
    check_state_a_builds(args.repo)
    check_zig_builds(args.repo)

    for n in notes:
        print("  ok    %s" % n)
    for p in problems:
        print("  FAIL  %s" % p, file=sys.stderr)
    if problems:
        print("\nverify_environment: %d problem(s)" % len(problems), file=sys.stderr)
        return 1
    print("\nverify_environment: %d checks passed" % len(notes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
