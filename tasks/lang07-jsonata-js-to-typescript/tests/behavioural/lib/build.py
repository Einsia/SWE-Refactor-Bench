#!/usr/bin/env python3
"""The `build` module: turn the submitted source into a probe, once.

Runs first. The fourteen modules after it read the ledger it writes into
`$SRB_SUITE_WORK`. One build rather than sixteen because a `tsc` build of a tree
this size takes the better part of a minute and sixteen of them would spend a
quarter of the stage's budget proving the same thing twice over.

What it measures, and why each is a measurement rather than setup
----------------------------------------------------------------
  - `npm run build` succeeds *as the submission's own package.json declares it*.
    Not `tsc` invoked by this module with flags of its own choosing, which would
    grade a build the submission does not ship. If the declared script is wrong,
    the submission does not build, and that is the finding rather than an
    inconvenience to work around.
  - It succeeds with no registry reachable. `vlib.base_env` sets
    `npm_config_offline`, so a submission that declared a dependency fails in a
    second with "cannot resolve" instead of spending the module's whole timeout
    against a route that does not exist.
  - It succeeds with `node_modules` absent. The tree it is built from has none and
    the contract asks for none: `tsc` is in the image, the original had no
    dependencies, and a port that needs a package tree cannot get one here.
  - The artifact is at `dist/probe.js`, under that name, and `node` loads it and
    answers `hello`.
  - A second build from the same tree emits byte-identical output. Measured here
    and *scored* by `structure` as `struct/build-idempotent`, because this module
    holds no points -- see the note on determinism below.

Why nothing is hidden from the build
------------------------------------
Every other task in this family shadows the old language's toolchain during the
build -- a C#-to-something port makes `dotnet` exit 127, so a port that shells out
to the original fails to build rather than passing on relayed answers. That move is
unavailable
here and its absence is the defining constraint of this task. The old language is
JavaScript and the new one compiles *to* JavaScript, so the runtime that would
serve a cheat is the runtime the submission legitimately needs. `node` cannot be
removed, shadowed, or restricted without removing the port's own feet.

So the weight this task's siblings put on a missing toolchain is carried
elsewhere, by three things that do not depend on being able to hide an
interpreter:

  - the reference tree is not in this image at all. `freeze.py` builds State A
    once at image build time, captures every answer, and the Dockerfile deletes
    the tree. There is nothing here to relay to.
  - the build runs in a copy with `dist/` deleted, so a committed artifact cannot
    stand in for a build that works. A port whose `dist/` its own source does not
    produce fails here rather than passing thirteen thousand cases against a
    prebuilt file.
  - `structure` runs the artifact with `dist/` alone in an empty directory and the
    tree it was built in deleted. A `dist/` that loads the JavaScript it was
    supposed to replace -- the one cheat this language pair makes easy -- stops
    answering there.

Determinism
-----------
The second build is *measured* here and scored in `structure`, which is a split
worth stating because it looks like an accident. This module is `required` and
worth zero: a submission that fails it is not graded further, and one that passes
it collects nothing. Points for reproducibility belong to a module that has some,
and the second build has to happen here anyway -- it needs `dist/` deleted and
restored around it, and doing that after fifteen other modules have started
reading the tree is not something a module can do politely.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import freeze
import vlib

# Names dropped from the copy: build output, installed packages, VCS metadata and
# editor caches. Neither `dist` nor `node_modules` is graded as a shipped file, and
# both are inputs a submission could use to smuggle in an artifact its source does
# not produce.
#
# `dist` is the important one. It is where the build writes, so it is *expected* to
# exist in a tree that has been built -- the instruction says so. Deleting it from
# the copy is what makes the build a measurement: what gets graded is what the
# source produces here, now, in this image.
DROP_FROM_COPY = {"dist", "node_modules", ".git", ".npm", ".cache", ".DS_Store",
                  "__pycache__", ".tsbuildinfo"}

# The three paths this suite and the freeze agree on. Imported rather than spelled
# again: `freeze.py` built the reference to the same contract, and a build module
# that looked for its artifact somewhere else would grade a different agreement
# than the one the expectations were captured under.
BUILD_OUTPUT = freeze.BUILD_OUTPUT      # "dist"
PROBE_JS = freeze.PROBE_JS              # "dist/probe.js"

HELLO_REQUEST = {"id": 1, "op": "hello"}

# Long enough for `tsc` over a tree this size on a loaded machine, twice. Not a
# graded threshold: a build that needs longer than this has failed to build, and
# the report says timed out rather than slow.
DEFAULT_BUILD_TIMEOUT = 1800.0


def node_binary(log) -> str:
    """The absolute path to `node`, resolved once, recorded in the ledger.

    Absolute, and not the bare name, because `structure` runs it from directories
    that have nothing to do with the build and invokes the interpreter through this
    string, so a bare `node` would make that check depend on the environment it
    inherited.

    Resolved once rather than per module for a second reason: fifteen modules
    compare bytes against expectations captured from one interpreter.  If two of
    them resolved `node` differently -- an image with more than one, a PATH edit --
    the differential would be measuring the runtimes against each other, and no
    report of this stage could survive that ambiguity.
    """
    found = shutil.which("node", path=vlib.base_env()["PATH"])
    if not found:
        # Not a submission failure. The image is expected to have node on the PATH
        # `base_env` pins, and if it does not, nothing in this stage can run.
        raise SystemExit(
            "build: node is not on the PATH base_env pins "
            f"({vlib.base_env()['PATH']}); the verifier image is incomplete, "
            "which is a harness fault and not a property of the submission")
    log.write(f"build: node resolved to {found}")
    return found


def _first_json_line(stdout: bytes) -> tuple[dict | None, str]:
    """The first line of stdout as an object, and what to say if it is not one."""
    head = stdout.strip().split(b"\n")[0] if stdout.strip() else b""
    if not head:
        return None, "the probe wrote nothing to stdout"
    try:
        payload = json.loads(head)
    except ValueError:
        return None, ("the first line of stdout is not JSON: "
                      + head[:400].decode("utf-8", "replace"))
    if not isinstance(payload, dict):
        return None, (f"the first line of stdout is a {type(payload).__name__}, "
                      f"not an object: "
                      + head[:400].decode("utf-8", "replace"))
    return payload, head[:1000].decode("utf-8", "replace")


def run(driver) -> int:
    """Build, smoke-test, measure the rebuild, write the ledger."""
    repo = driver.repo
    scratch = driver.work / "build"
    build_dir = scratch / "src"
    ledger_path = driver.shared / "build-ledger.json"
    ledger: dict = {"ok": False, "summary": ""}

    def finish(status: str, summary: str) -> int:
        """Write the ledger and emit. Every exit from this module goes through it.

        The ledger is written on the failure paths too, carrying `ok: false` and a
        reason. `driver.probe_prefix` reads that reason and quotes it, so fifteen
        modules say "the build did not produce a probe: <why>" instead of fifteen
        variations on a missing file.
        """
        ledger["summary"] = summary
        driver.shared.mkdir(parents=True, exist_ok=True)
        vlib.write_json(ledger_path, ledger)
        driver.metadata["ledger"] = str(ledger_path)
        return driver.emit(status, summary)

    node = node_binary(driver.log)
    ledger["node"] = node

    # -- the copy ----------------------------------------------------------
    if not repo.is_dir():
        driver.add("build/repo-present", False,
                   f"the submission directory {repo} does not exist",
                   required=True, verdict="error")
        return finish("error", "no submission tree")

    build_dir.parent.mkdir(parents=True, exist_ok=True)
    copied = vlib.copy_tree(repo, build_dir, skip_names=DROP_FROM_COPY)
    driver.metadata["files_copied"] = copied
    driver.add("build/source-copied", copied > 0,
               f"copied {copied} file(s) to scratch, excluding "
               f"{sorted(DROP_FROM_COPY)}",
               detail="The build runs on a copy with dist/ and node_modules "
                      "removed, so neither a committed build artifact nor an "
                      "installed package tree can stand in for a build that works.")
    if copied == 0:
        return finish("error", "the submission tree is empty")

    package_json = build_dir / "package.json"
    if not package_json.is_file():
        driver.add("build/package-json", False,
                   "package.json is absent, so there is no declared build script",
                   required=True,
                   detail="The build contract requires `npm run build` to be the "
                          "command that produces dist/probe.js. package.json is in "
                          "the tree the agent received and is listed as a preserved "
                          "path; a submission that removed it removed the entry "
                          "point.")
        return finish("error", "no package.json")

    try:
        manifest = json.loads(package_json.read_text(encoding="utf-8"))
    except ValueError as exc:
        driver.add("build/package-json", False,
                   f"package.json does not parse: {exc}", required=True)
        return finish("error", "package.json does not parse")

    scripts = manifest.get("scripts") or {}
    declared = scripts.get("build", "") if isinstance(scripts, dict) else ""
    driver.add("build/package-json", bool(declared),
               f"package.json declares scripts.build = {declared!r}"
               if declared else "package.json declares no `build` script",
               required=True,
               detail="Whatever this script is, it is what runs. No check here "
                      "grades its text: `npm run build` is the whole interface, "
                      "and a port is free to invoke the compiler however it likes "
                      "so long as the result is dist/probe.js.")
    driver.metadata["declared_build"] = declared
    if not declared:
        return finish("error", "no build script declared")

    # -- the build ---------------------------------------------------------
    env = vlib.base_env()
    timeout = float(os.environ.get("SRB_BUILD_TIMEOUT", DEFAULT_BUILD_TIMEOUT))
    result = vlib.run(["npm", "run", "build"], cwd=build_dir, env=env,
                      timeout=timeout, log=driver.log, label="npm run build")
    log_path = driver.work / "build.log"
    log_path.write_bytes(result.stdout + b"\n--- stderr ---\n" + result.stderr)
    driver.metadata["build_log"] = str(log_path)
    driver.metadata["build_seconds"] = round(result.duration, 1)

    driver.add("build/build-succeeds", result.ok,
               f"`npm run build` completed in {result.duration:.0f}s"
               if result.ok else
               f"`npm run build` failed (exit {result.returncode}"
               f"{', timed out' if result.timed_out else ''})",
               required=True,
               detail=result.tail(lines=40, limit=4000))
    if not result.ok:
        return finish("error", f"the build failed: {result.tail(lines=6)}")

    probe_js = build_dir / PROBE_JS
    dist = build_dir / BUILD_OUTPUT
    driver.add("build/build-artifact", probe_js.is_file(),
               f"{PROBE_JS} {'exists' if probe_js.is_file() else 'is absent'} "
               f"after the build",
               required=True,
               detail="The directory and the filename are both part of the "
                      "contract: every module in this suite invokes "
                      "`node dist/probe.js`, and so does the verification stage.")
    if not probe_js.is_file():
        emitted = sorted(
            str(p.relative_to(build_dir))
            for p in dist.rglob("*") if p.is_file()) if dist.is_dir() else []
        driver.metadata["dist_contents"] = emitted[:60]
        return finish("error", f"the build produced no {PROBE_JS}")

    dist_files = sorted(str(p.relative_to(build_dir))
                        for p in dist.rglob("*") if p.is_file())
    driver.metadata["dist_files"] = dist_files[:200]
    ledger["dist_files"] = dist_files[:400]

    # -- the smoke test ----------------------------------------------------
    # `hello` and nothing more. This module answers "is there a probe here"; a
    # wrong answer to any real question belongs to the module that owns the family
    # it was asked in, where the report can say which family and how many.
    hello = vlib.run([node, str(probe_js)], cwd=build_dir, env=env,
                     stdin_data=(json.dumps(HELLO_REQUEST) + "\n").encode(),
                     timeout=60.0, log=driver.log, label="hello")
    payload, detail = _first_json_line(hello.stdout)
    answered = bool(payload and payload.get("ok") is True
                    and payload.get("id") == 1)
    if not hello.stdout.strip():
        detail = hello.tail(lines=20, limit=2000)
    driver.add("build/probe-answers-hello", answered,
               "the built probe answered the `hello` handshake" if answered else
               "the built probe did not answer `hello` with a success envelope",
               required=True, detail=detail)
    if not answered:
        return finish("error", "the built probe does not answer hello")

    # -- the second build --------------------------------------------------
    # Compared per emitted file rather than on `dist/probe.js` alone: a build that
    # embeds a timestamp usually embeds it in the declarations or in a module the
    # probe does not reach, and `structure` compares whole trees.
    first = {rel: vlib.sha256_file(build_dir / rel) for rel in dist_files}
    backup = scratch / "dist-first"
    if backup.exists():
        shutil.rmtree(backup)
    shutil.copytree(dist, backup, symlinks=True)
    shutil.rmtree(dist)
    second = vlib.run(["npm", "run", "build"], cwd=build_dir, env=env,
                      timeout=timeout, log=driver.log, label="npm run build (2)")

    changed: list[str] = []
    rebuild_detail = ""
    if not second.ok:
        # `changed` stays empty on purpose. The list means "these emitted files
        # differ", and a build that did not finish emitted nothing to compare;
        # filling it with every filename would report a diff that was never taken.
        # `structure` has a branch for exactly this shape -- no changed files and
        # not deterministic -- and says the second build did not reproduce the first.
        rebuild_detail = (f"the second build exited {second.returncode}"
                          f"{' (timed out)' if second.timed_out else ''}: "
                          f"{second.tail(lines=8)}")
    else:
        emitted_now = {str(p.relative_to(build_dir))
                       for p in dist.rglob("*") if p.is_file()}
        differing = [rel for rel in first
                     if (build_dir / rel).is_file()
                     and vlib.sha256_file(build_dir / rel) != first[rel]]
        missing = [rel for rel in first if rel not in emitted_now]
        extra = sorted(emitted_now - set(first))
        changed = sorted(set(differing) | set(missing) | set(extra))
        notes = []
        if differing:
            notes.append(f"{len(differing)} file(s) differ in content")
        if missing:
            notes.append(f"{len(missing)} file(s) the first build emitted are "
                         f"absent: {missing[:10]}")
        if extra:
            notes.append(f"{len(extra)} file(s) the first build did not emit: "
                         f"{extra[:10]}")
        rebuild_detail = "; ".join(notes)

    ledger["rebuild_deterministic"] = bool(second.ok and not changed)
    ledger["rebuild_changed"] = changed[:40]
    ledger["rebuild_files"] = len(first)
    ledger["rebuild_detail"] = rebuild_detail

    # Restore the first build's output. The fourteen modules after this one grade the
    # artifact that answered `hello`, not the second attempt -- otherwise a
    # non-reproducible build would be charged once here and then silently change
    # what every other module measured.
    if dist.is_dir():
        shutil.rmtree(dist)
    shutil.copytree(backup, dist, symlinks=True)
    shutil.rmtree(backup)
    restored = vlib.sha256_file(probe_js)
    if restored != first.get(PROBE_JS):
        raise AssertionError(
            f"build: restoring the first build left {PROBE_JS} at "
            f"{restored[:16]}, not {first.get(PROBE_JS, '')[:16]}; the tree "
            f"fourteen modules are about to read is not the tree that was measured")

    version = vlib.run([node, "--version"], cwd=build_dir, env=env,
                       timeout=30.0, log=driver.log, label="node --version")
    ledger.update({
        "ok": True,
        "build_dir": str(build_dir),
        "probe_js": str(probe_js),
        "dist_dir": str(dist),
        "node_version": version.text().strip(),
        "tsc": declared,
        "probe_digest": first.get(PROBE_JS, ""),
        "build_seconds": round(result.duration, 1),
        "env_note": "built offline from a copy with dist/ and node_modules "
                    "removed; nothing was hidden from PATH, because the old "
                    "language's runtime is also the new one's -- see the module "
                    "docstring",
    })
    driver.metadata["probe_digest"] = ledger["probe_digest"]
    driver.metadata["node_version"] = ledger["node_version"]
    driver.metadata["rebuild"] = {
        "deterministic": ledger["rebuild_deterministic"],
        "files": ledger["rebuild_files"],
        "changed": len(changed),
    }
    return finish("ok", f"built in {result.duration:.0f}s, {len(dist_files)} "
                        f"emitted file(s); the probe answers hello")
