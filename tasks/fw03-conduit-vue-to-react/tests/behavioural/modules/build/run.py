#!/usr/bin/env python3
"""Build the submission, build nothing else, then drive both bundles.

This module is why the nine after it can be pure comparisons. It runs first, it
is required, and it publishes into ``$SRB_SUITE_WORK`` the only thing the other
modules share: two observation files recorded under one nonce.

    1. copy the submission, leaving behind node_modules/ and any dist/ that
       arrived with it -- so a submission cannot be graded on a bundle it
       prepared by hand, or on a node_modules/ it left with something
       interesting in it
    2. install offline, from whichever of the two closures the manifest asks for:
       npm against the allowlist cache, yarn against a mirror minted from the
       lockfile State A ships
    3. run the project's own `build` script
    4. look at what the build produced, and record which stack it was
    5. mint a nonce, unpack the State A bundle, and drive the same 100 scenarios
       through the same driver against both bundles under that nonce

Step 5 is the part that makes the rest hard to work around. The nonce is stamped
through every title, description, body, bio, tag and comment the mock API serves,
so the strings both bundles have to render did not exist when the submission was
written, and neither side has any way to learn the value. State A's side is not a
recording -- it is what the State A bundle does, here, this run, against exactly
the data the submission's bundle was given.

What step 2 deliberately does NOT do is refuse to install Vue. A cache holding
the allowlist and nothing else would kill a manifest still asking for a Vue
package on npm's ENOTCACHED, and a build check that fails zeroes the stage -- so
feeding State A back unchanged would score 0, in a stage whose
whole question is what the application does, about the one submission guaranteed
to do it correctly. Whether the port happened is a question for the audit
stage, and the two checks here that ask it (`ported-off-vue`, `react-present`)
carry weight 0: recorded and reported, unable to move a score. That is the same
shape fw02 uses for `X-Powered-By`.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
from contextlib import contextmanager
from pathlib import Path

from swerefactor.contract import submission_env

REPO = Path(os.environ.get("SRB_REPO", "/workspace/repo"))
SUITE = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"))
WORK = Path(os.environ.get("SRB_WORK", "/tmp/srb/build"))
SUITE_WORK = Path(os.environ.get("SRB_SUITE_WORK", "/tmp/srb/shared"))
RESULT = Path(os.environ.get("SRB_RESULT", str(WORK / "result.json")))

BUILD_DIR = SUITE_WORK / "candidate"
ORACLE_DIR = SUITE_WORK / "oracle"
REFERENCE_OUT = SUITE_WORK / "reference-observation.json"
CANDIDATE_OUT = SUITE_WORK / "candidate-observation.json"

OBSERVE = SUITE / "lib" / "harness" / "observe.mjs"
ORACLE_TARBALL = SUITE / "data" / "oracle-dist.tgz"

# Never copied out of the submission. `dist` is on the list because the whole
# point is that it gets rebuilt: a committed bundle is not a build.
SCRUB_DIRS = {
    "node_modules", "dist", "build", "out", ".vite", ".cache", ".parcel-cache",
    ".next", ".nuxt", ".turbo", "coverage", ".output", ".git",
}

# Lockfiles for package managers whose offline cache was never minted. yarn.lock
# is NOT on this list: State A ships one, the image mints an offline mirror from
# it, and installing it with npm instead is not a stricter test of the same tree
# -- npm cannot read yarn.lock, so it re-resolves 25 semver ranges and installs a
# closure State A never had.  A diagnosis of "eslint plugins unresolvable under
# npm 10 hoisting" is a diagnosis of that substituted tree, not of this one.
FOREIGN_LOCKFILES = ("pnpm-lock.yaml", "bun.lockb", "bun.lock")

# Declared to yarn through the environment rather than a .yarnrc, because the
# only file that would work is one inside the tree being graded, and the
# submission may ship its own.  Measured: yarn 1.22.22 reads
# YARN_YARN_OFFLINE_MIRROR, and refuses the install without it.
YARN_MIRROR = os.environ.get("SRB_YARN_MIRROR", "/opt/yarn-mirror")

INSTALL_TIMEOUT = 900
BUILD_TIMEOUT = 900
DRIVE_TIMEOUT = 2400

# webpack 4 asks OpenSSL for md4 to hash chunk ids; OpenSSL 3 removed it from the
# default provider. The flag is not a build-quality judgement, it is what the era
# of the toolchain requires, and `build` reaches for it only after the plain
# invocation has already failed with this exact error.
OPENSSL_LEGACY_MARKERS = ("ERR_OSSL_EVP_UNSUPPORTED", "error:0308010C",
                          "digital envelope routines::unsupported")

# What a Conduit release contains. The manifest and the service worker are here
# because State A shipped a PWA, and dropping it is a silent regression in the
# release artefact rather than a change to any screen.
ARTIFACT_PATTERNS = (
    ("*.js", "at least one JavaScript bundle"),
    ("manifest.json|manifest.webmanifest|*.webmanifest", "the PWA manifest"),
    ("*service-worker*.js|sw.js|*workbox*.js|registerSW.js", "a service worker"),
)

CHECKS: list[dict] = []


def record(cid, ok, summary, *, weight=1.0, required=False, detail="", verdict=None):
    entry = {
        "id": cid,
        "verdict": verdict or ("pass" if ok else "fail"),
        "weight": weight,
        "summary": summary,
    }
    if required:
        entry["required"] = True
    if detail:
        entry["detail"] = detail[-4000:]
    CHECKS.append(entry)
    print(f"{entry['verdict']:5} {cid}: {summary}", flush=True)
    return ok


def finish(status=None, summary="", metadata=None) -> int:
    payload: dict = {"checks": CHECKS}
    if status:
        payload["status"] = status
        payload["summary"] = summary
    if metadata:
        payload["metadata"] = metadata
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    ok = all(c["verdict"] == "pass" for c in CHECKS) and not status
    return 0 if ok else 1


def run(cmd, cwd, timeout, env=None):
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), timeout=timeout, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        return proc.returncode, proc.stdout, time.time() - started
    except subprocess.TimeoutExpired as exc:
        out = exc.output or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return 124, f"{out}\n[timed out after {timeout}s]", time.time() - started
    except FileNotFoundError as exc:
        return 127, str(exc), time.time() - started


def tail(text, lines=40):
    return "\n".join((text or "").rstrip().split("\n")[-lines:])


def npm_env(**extra):
    env = submission_env()
    env.update({
        "NPM_CONFIG_OFFLINE": "true",
        "NPM_CONFIG_AUDIT": "false",
        "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "CI": "1",
        # Both package managers point at what the image minted for them: npm at
        # the allowlist cache, yarn at the mirror built from State A's lockfile.
        "YARN_YARN_OFFLINE_MIRROR": YARN_MIRROR,
        # The build's own clock and locale, fixed: a bundle that stamps a date
        # must not differ between the two sides for that reason.
        "TZ": "UTC",
        "LC_ALL": "C",
    })
    # NODE_ENV is REMOVED, not set, and the difference is the whole point.
    #
    # Every toolchain a submission here can ship already defaults its build to
    # production: `vue-cli-service build` does, and so does `vite build`.  Leaving
    # the name absent lets each pick that default.  Setting it is wrong in both
    # directions -- "production" makes npm and yarn skip devDependencies, and the
    # bundler itself is a devDependency in both stacks, so install stops producing
    # a build; and the empty string is worse than either, because it is a value but
    # is not "production", so vue-cli-service takes its development path.
    #
    # Measured on State A in this image, install and build both rc=0 either way,
    # so the difference is only visible in the output shape:
    #
    #   NODE_ENV absent      49 files, dist/js/app.c9c406d4.js (hashed, under js/),
    #                        dist/service-worker.js PRESENT, workbox-*.js emitted
    #   NODE_ENV set-empty   33 files, dist/app.js + dist/0.js..14.js (flat and
    #                        unhashed), NO service worker, no workbox at all;
    #                        dist/manifest.json still written, so `pwa-manifest`
    #                        passes and only `service-worker` fails
    #
    # That failure is weight 1.0, which puts the identity property at 21768/21773
    # instead of 21769/21773 and takes the `build` module off rate 1.0.
    #
    # task.toml USED TO declare NODE_ENV = "" in [verifier.env] and in
    # [environment.env]; the commit that added this pop removed both, so the name
    # no longer arrives from the task at all.  The pop stays anyway, because
    # nothing in this repository defines what an empty *declared* value means --
    # Harbor decides, and Harbor is not here -- and because `docker run -e
    # NODE_ENV=` passes the name through whether or not a toml mentions it.  A
    # grade must not turn on that reading, so the module does not let it.
    env.update({k: v for k, v in extra.items() if v is not None})
    # After the extras, not before: the guarantee is that whatever arrives, from
    # the image, the harness or a caller, the build is handed an environment with
    # no NODE_ENV in it.  Popping first would leave a caller's kwarg standing and
    # make the sentence above false.  No caller passes one today (the only extra
    # in this module is NODE_OPTIONS) -- this is so that none can.
    env.pop("NODE_ENV", None)
    return env


@contextmanager
def yarn_lock_aside(build_dir: Path, hide: bool):
    """Hide yarn.lock for the duration of an npm attempt.

    npm 7+ does not ignore a yarn.lock it finds; arborist reads it as a source of
    resolutions. So a submission that ported to React but left State A's lockfile
    in the tree -- the shape instruction.md warns about, which means the shape that
    will arrive -- has its npm install silently steered onto State A's pins.
    Measured here: with the file present, `npm install --offline` resolves
    is-buffer to 2.0.3 and dies on ENOTCACHED; with it moved aside, 2.0.5, and the
    install succeeds. Nothing about that failure is a fact about the submission.

    Set aside rather than deleted, and only for the attempt: the tree is a copy, but
    a later check that reads it should see what was submitted.
    """
    lock = build_dir / "yarn.lock"
    if not hide or not lock.is_file():
        yield False
        return
    stashed = build_dir / "yarn.lock.srb-aside"
    lock.rename(stashed)
    try:
        yield True
    finally:
        stashed.rename(lock)


def install_plans(build_dir: Path) -> list[tuple[str, list[str]]]:
    """The install commands to try, in order.

    npm first when there is an npm lockfile, because `npm ci` installs exactly
    that lockfile and refuses to run when it disagrees with the manifest -- which
    is information worth having.  yarn when there is a yarn lockfile, because that
    is the one State A ships.  A submission carrying both gets npm; one carrying a
    yarn.lock its rewritten manifest no longer matches falls through to
    `npm install`, which resolves from the manifest and ignores the stale file.
    """
    npm_flags = ["--offline", "--no-audit", "--no-fund", "--loglevel=error"]
    yarn_flags = ["--offline", "--frozen-lockfile", "--non-interactive",
                  "--no-progress"]
    plans: list[tuple[str, list[str]]] = []
    if (build_dir / "package-lock.json").is_file():
        plans.append(("npm ci", ["npm", "ci", *npm_flags]))
    if (build_dir / "yarn.lock").is_file():
        plans.append(("yarn install --frozen-lockfile",
                      ["yarn", "install", *yarn_flags]))
    if not (build_dir / "package-lock.json").is_file():
        plans.append(("npm install", ["npm", "install", *npm_flags]))
    return plans


def install(build_dir: Path) -> tuple[bool, str, str, list[str]]:
    """Install, and report which attempt worked and what it needed.

    Each plan is tried as the project wrote it, then once more with scripts
    withheld.  The retry exists for one measured reason: node-sass 4.14.1's
    postinstall reaches node-gyp 3.8.0 for python2 and no binary for node 20's
    ABI was ever published -- for a dependency this tree imports from zero .scss
    files.  It is a retry rather than a default because a submission whose
    postinstall does real work must get to run it, and because needing it is
    worth recording.
    """
    attempts: list[str] = []
    for label, cmd in install_plans(build_dir):
        for withhold_scripts in (False, True):
            full = cmd + (["--ignore-scripts"] if withhold_scripts else [])
            how = label + (" --ignore-scripts" if withhold_scripts else "")
            # A failed install leaves a partial tree; the next attempt must not
            # inherit it, or "installed" would mean two half-installs merged.
            shutil.rmtree(build_dir / "node_modules", ignore_errors=True)
            with yarn_lock_aside(build_dir, cmd[0] == "npm") as hidden:
                code, out, secs = run(full, build_dir, INSTALL_TIMEOUT, npm_env())
            if hidden:
                how += " (yarn.lock set aside)"
            if code == 0:
                return True, how, f"installed offline in {secs:.0f}s via {how}", attempts
            attempts.append(f"{how}: exit {code} after {secs:.0f}s\n{tail(out, 25)}")
    return False, "", "", attempts


def copy_submission() -> tuple[bool, str]:
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR, ignore_errors=True)
    BUILD_DIR.parent.mkdir(parents=True, exist_ok=True)
    skipped: set[str] = set()

    def ignore(directory, names):
        drop = {n for n in names if n in SCRUB_DIRS}
        skipped.update(drop)
        return drop

    try:
        shutil.copytree(REPO, BUILD_DIR, ignore=ignore, symlinks=True)
    except OSError as exc:
        return False, f"could not copy the submission: {exc}"
    left = ", ".join(sorted(skipped)) or "nothing to leave behind"
    return True, f"copied to a clean tree, leaving behind: {left}"


def matches(root: Path, pattern: str) -> list[str]:
    hits: list[str] = []
    for alternative in pattern.split("|"):
        for found in root.rglob(alternative):
            if found.is_file():
                hits.append(str(found.relative_to(root)))
    return sorted(set(hits))


def find_dist(root: Path) -> Path | None:
    for name in ("dist", "build", "out", ".output/public", "public/build"):
        candidate = root / name
        if (candidate / "index.html").is_file():
            return candidate
    for found in sorted(root.glob("*/index.html")):
        if "node_modules" not in found.parts:
            return found.parent
    return None


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    SUITE_WORK.mkdir(parents=True, exist_ok=True)

    # -- 1. a clean copy ---------------------------------------------------- #
    ok, summary = copy_submission()
    if not record("clean-tree", ok, summary, required=True):
        return finish()

    manifest_path = BUILD_DIR / "package.json"
    if not manifest_path.is_file():
        record("clean-tree", False, "no package.json at the repository root",
               required=True)
        return finish()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        record("install", False, f"package.json is not valid JSON: {exc}",
               required=True)
        return finish()

    scripts = manifest.get("scripts") or {}
    if "build" not in scripts:
        record("install", False,
               "package.json declares no 'build' script, so there is no way to "
               "produce a release from source", required=True)
        return finish()

    # -- 2. install, offline ------------------------------------------------ #
    foreign = [f for f in FOREIGN_LOCKFILES if (BUILD_DIR / f).is_file()]
    ok, how, summary, attempts = install(BUILD_DIR)
    if not ok:
        joined = "\n\n".join(attempts)
        hint = ""
        if "ENOTCACHED" in joined:
            hint += (
                "\nENOTCACHED: the manifest asks npm for a package that is not in "
                "the offline cache, which was minted from the allowlist."
            )
        if "Can't make a request in offline mode" in joined:
            hint += (
                "\nyarn wanted a tarball the offline mirror does not hold. The "
                "mirror was minted from the lockfile State A ships, so a rewritten "
                "manifest needs a lockfile written by the package manager that "
                "matches it."
            )
        if foreign:
            hint += (
                "\nFound " + ", ".join(foreign) + ". This stage installs with npm "
                "or yarn; no offline cache was minted for those, so their lockfiles "
                "were not read."
            )
        record("install", False,
               "no offline install succeeded: "
               + "; ".join(a.split(":", 1)[0] for a in attempts),
               required=True, detail=hint + "\n\n" + joined)
        return finish()
    record("install", True, summary)
    if "--ignore-scripts" in how:
        record("install-ran-scripts", False,
               "the install succeeded only with lifecycle scripts withheld; a "
               "postinstall in this dependency closure cannot run here",
               weight=0.0,
               detail="\n\n".join(attempts)
                      + "\n\nRecorded, not charged: State A's own closure needs "
                        "this (node-sass 4.14.1 -> node-gyp 3.8.0 -> python2), and "
                        "the packages whose scripts were withheld are the ones "
                        "State A also could not run here.")

    # -- 3. the project's own build ---------------------------------------- #
    code, out, secs = run(["npm", "run", "build", "--silent"],
                          BUILD_DIR, BUILD_TIMEOUT, npm_env())
    legacy_openssl = False
    if code != 0 and any(m in out for m in OPENSSL_LEGACY_MARKERS):
        first = tail(out)
        legacy_openssl = True
        code, out, secs = run(
            ["npm", "run", "build", "--silent"], BUILD_DIR, BUILD_TIMEOUT,
            npm_env(NODE_OPTIONS="--openssl-legacy-provider"))
        if code == 0:
            record("build-needs-legacy-openssl", False,
                   "the build only succeeds with --openssl-legacy-provider: "
                   "webpack 4 hashes with md4, which OpenSSL 3 removed",
                   weight=0.0, detail=first)
        else:
            out = first + "\n\n[retried with --openssl-legacy-provider]\n" + tail(out)
    if code != 0:
        record("build", False,
               f"`npm run build` failed with exit {code} after {secs:.0f}s",
               required=True, detail=tail(out))
        return finish()
    record("build", True, f"built in {secs:.0f}s"
           + (" with --openssl-legacy-provider" if legacy_openssl else ""))

    # -- 4. what the build produced ----------------------------------------- #
    dist = find_dist(BUILD_DIR)
    if dist is None:
        record("release-exists", False,
               "the build produced no directory containing an index.html",
               required=True)
        return finish()
    rel = dist.relative_to(BUILD_DIR)
    files = sum(1 for p in dist.rglob("*") if p.is_file())
    record("release-exists", True, f"release in {rel}/ ({files} files)",
           required=True)

    html = (dist / "index.html").read_text(encoding="utf-8", errors="replace")
    record("entry-document", "<script" in html.lower(),
           "index.html loads a script"
           if "<script" in html.lower() else
           "index.html loads no script, so it cannot boot a single-page app")

    for pattern, description in ARTIFACT_PATTERNS:
        hits = matches(dist, pattern)
        cid = {"*.js": "bundle"}.get(pattern) or (
            "pwa-manifest" if "manifest" in pattern else "service-worker")
        record(cid, bool(hits),
               f"{description}: {', '.join(hits[:3])}" if hits else
               f"the release has no {description} (no match for '{pattern}')",
               required=(cid == "bundle"),
               detail="" if hits else
               "State A shipped a PWA; dropping it is a regression in the "
               "release artefact rather than a change to any screen.")

    # -- 4b. which stack was shipped, at weight 0 --------------------------- #
    # Measured from the manifest that was actually installed from, and reported.
    # Both are weight 0 on purpose: this stage grades what the application does,
    # and the port is judged by the audit stage, which can read the diff, the
    # history and the agent's own account of the work. A behaviour suite that
    # zeroed a correct application for naming the wrong dependency would be
    # answering a question it cannot see the evidence for.
    declared = {}
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        declared.update(manifest.get(section) or {})
    retired = sorted(name for name in declared
                     if name in ("vue", "vue-router", "vuex", "vue-axios",
                                 "vue-template-compiler", "@vue/compat")
                     or name.startswith("@vue/"))
    record("ported-off-vue", not retired,
           "the manifest declares no Vue package"
           if not retired else
           "the manifest still declares: " + ", ".join(retired),
           weight=0.0,
           detail="" if not retired else
           "Weight 0: recorded for the report and for the audit stage, which "
           "judges whether the port happened. This stage scores behaviour, and a "
           "submission that still ships Vue is graded on what it renders like any "
           "other.")
    react = sorted(n for n in ("react", "react-dom") if n in declared)
    record("react-present", len(react) == 2,
           "the manifest declares " + ", ".join(react) if react else
           "the manifest declares neither react nor react-dom",
           weight=0.0)

    # -- 5. drive both bundles under one nonce ------------------------------ #
    if ORACLE_DIR.exists():
        shutil.rmtree(ORACLE_DIR, ignore_errors=True)
    ORACLE_DIR.mkdir(parents=True)
    try:
        with tarfile.open(ORACLE_TARBALL) as tf:
            tf.extractall(ORACLE_DIR)
    except (OSError, tarfile.TarError) as exc:
        return finish(status="error",
                      summary=f"could not unpack the State A bundle from "
                              f"{ORACLE_TARBALL}: {exc}. This is a grader-side "
                              f"failure, not the submission's.")
    oracle_dist = ORACLE_DIR / "dist-stateA"
    if not (oracle_dist / "index.html").is_file():
        return finish(status="error",
                      summary=f"no index.html in the unpacked State A bundle at "
                              f"{oracle_dist}. This is a grader-side failure, not "
                              f"the submission's.")

    # Settable so a reported failure can be reproduced exactly:
    #   SRB_NONCE=<the one in the report> swerefactor behavioural ...
    nonce = os.environ.get("SRB_NONCE", "").strip() or secrets.token_hex(4)
    print(f"\nnonce for this run: {nonce}", flush=True)

    def drive(dist_dir: Path, out_path: Path, label: str):
        return run(
            ["node", str(OBSERVE), str(dist_dir), str(out_path), "--nonce", nonce],
            SUITE, DRIVE_TIMEOUT, dict(os.environ, TZ="UTC", LC_ALL="C.UTF-8"),
        )

    code, out, secs = drive(oracle_dist, REFERENCE_OUT, "State A")
    (WORK / "drive-reference.log").write_text(out, encoding="utf-8")
    if code != 0 or not REFERENCE_OUT.is_file():
        return finish(status="error",
                      summary=f"driving the State A bundle failed with exit "
                              f"{code} after {secs:.0f}s. This is a grader-side "
                              f"failure, not the submission's; the log is at "
                              f"{WORK / 'drive-reference.log'}.")
    reference = json.loads(REFERENCE_OUT.read_text(encoding="utf-8"))
    ref_clean = [s for s in reference["scenarios"]
                 if s.get("observed") and not s.get("stepErrors")]
    if len(ref_clean) != len(reference["scenarios"]) or not ref_clean:
        broken = [s["id"] for s in reference["scenarios"]
                  if not s.get("observed") or s.get("stepErrors")]
        return finish(status="error",
                      summary=f"the State A bundle could not be driven through "
                              f"{len(broken)} of {len(reference['scenarios'])} "
                              f"scenarios ({', '.join(broken[:5])}). This is a "
                              f"grader-side failure, not the submission's.")
    print(f"State A: {len(ref_clean)} scenarios in {secs:.0f}s", flush=True)

    code, out, secs = drive(dist, CANDIDATE_OUT, "submission")
    (WORK / "drive-candidate.log").write_text(out, encoding="utf-8")
    if not CANDIDATE_OUT.is_file():
        record("observable", False,
               f"the submission's bundle could not be driven at all (exit {code} "
               f"after {secs:.0f}s); no observation was written",
               required=True, detail=tail(out, 60))
        return finish(metadata={"nonce": nonce})
    candidate = json.loads(CANDIDATE_OUT.read_text(encoding="utf-8"))
    cand_clean = [s for s in candidate["scenarios"]
                  if s.get("observed") and not s.get("stepErrors")]
    # Not "all 100 succeeded" -- a scenario the submission cannot be driven
    # through is scored by the module that owns it, one failing check per node it
    # did not render. What is required here is that *something* rendered, because
    # a bundle that renders nothing anywhere leaves nothing to compare.
    record("observable", bool(cand_clean),
           f"driven through {len(cand_clean)}/{len(candidate['scenarios'])} "
           f"scenarios in {secs:.0f}s",
           required=True,
           detail="" if cand_clean else tail(out, 60))

    return finish(metadata={
        "nonce": nonce,
        "dist": str(rel),
        "release_files": files,
        "install": how,
        "legacy_openssl": legacy_openssl,
        "scenarios_reference": len(ref_clean),
        "scenarios_candidate": len(cand_clean),
    })


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # a crash here must still leave a readable result
        import traceback
        record("build", False, f"the build module raised {type(exc).__name__}: {exc}",
               required=True, detail=traceback.format_exc())
        sys.exit(finish())
