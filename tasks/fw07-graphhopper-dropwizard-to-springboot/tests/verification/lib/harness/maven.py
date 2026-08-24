"""Building a tree — either tree — and finding the jar that came out.

Both sides are built by the same function with the same command, because a
comparison in which the two sides were built differently is not a comparison of
the two sides.  State A gets no special handling anywhere in this file.

The interesting problem is finding the artefact.  State A produces
`web/target/graphhopper-web-11.0-SNAPSHOT.jar` because that is what its pom
names, and a submission is under no obligation to keep that name — a rewrite
that renames the module, changes the artifactId, or relocates the executable jar
has done nothing wrong.  So the jar is DISCOVERED: every candidate under
`target/` that is executable (has a Main-Class, or is a Spring Boot repackage)
is collected, and the largest is taken.

Largest, not first: Spring Boot's repackaging leaves the thin original beside the
fat jar as `*.jar.original`, and shade leaves `original-*.jar`.  Those are
excluded by name, and where a submission produces several genuine candidates the
fat one is the runnable one.

Discovery by size is only safe on a tree whose `target/` directories came out of
this build, which is what `scrub_build_output` is for.  See its docstring: it is
the difference between "the largest jar this build produced" and "the largest jar
in the tree", and a submission is free to commit the second one.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

from swerefactor.contract import submission_env

MVN = shutil.which("mvn") or "/opt/maven/bin/mvn"
_M2_REPO = os.environ.get("SRB_M2_REPO", "/root/.m2/repository")
_SETTINGS = os.environ.get("SRB_OFFLINE_SETTINGS",
                           "/opt/srb/settings-offline.xml")

# The instant every tree is stamped to before it is built.  Shared with the
# environment image; see `normalise_mtimes`.
_FIXED_MTIME = "2024-01-01T00:00:00Z"

# Excluded by name.  Each of these is a by-product that a naive "first jar with a
# Main-Class" search would pick up and fail to launch.
_BYPRODUCT_SUFFIXES = (".jar.original",)
_BYPRODUCT_PREFIXES = ("original-",)


class BuildFailed(RuntimeError):
    def __init__(self, message: str, log: str):
        super().__init__(message)
        self.log = log


def scrub_build_output(tree: Path) -> list[str]:
    """Delete every Maven output directory in `tree`, and say which ones.

    Called on BOTH sides before either is built, which is the whole point.

    What it prevents: `find_app_jar` takes the largest launchable jar under any
    `target/`, and a tree is a submission's to populate.  A submission that
    committed a fat jar — the one it was handed, or one built from a tree it kept
    aside — into `web/target/` would have that jar discovered and graded, while
    the sources actually submitted contributed nothing but a build that had to
    succeed.  `mvn package` does not remove it: `package` is not `clean`, and a
    jar sitting in `target/` that this reactor does not overwrite is left exactly
    where it was found.

    Why not `mvn clean` instead: `clean` is a lifecycle phase, so it runs the
    tree's own build to find out what to delete.  A submission whose pom fails to
    parse would fail the clean and never reach the package, and the failure would
    be reported as the wrong thing.  `target/` beside a `pom.xml` is Maven's own
    convention, does not need the tree's cooperation to enumerate, and cannot be
    redirected by a pom this function never reads.

    Only directories named `target` that sit BESIDE a `pom.xml`: that is where
    Maven writes, and a source directory that happens to be called `target`
    somewhere in a test fixture is not build output.  `.git` and `.mvn` are left
    alone — a build may legitimately read either, and neither can carry a jar
    this function's caller would then select.
    """
    removed = []
    for pom in sorted(tree.rglob("pom.xml")):
        out = pom.parent / "target"
        if out.is_dir() and not out.is_symlink():
            shutil.rmtree(out, ignore_errors=True)
            removed.append(str(out.relative_to(tree)))
    return removed


def normalise_mtimes(tree: Path) -> int:
    """Give every path in `tree` one fixed, non-zero mtime.  Returns how many
    paths were at epoch 0 before the re-stamp.

    Called on BOTH sides before either is built, for the same reason
    `scrub_build_output` is: the two trees must arrive at Maven in the same
    condition.  Only one of them normally needs it, which is exactly why it
    belongs here rather than at the point where that one is unpacked -- a
    re-stamp attached to one side's unpack leaves the other side's epoch-0 files
    in place, and the failure below is silent on whichever side keeps them.

    What it prevents, and the failure is silent.  The snapshot builder fixes every
    mtime in original.tar.gz at epoch 0 so the archive's bytes depend only on its
    content.  maven-resources-plugin copies an UNFILTERED resource only when
    `source.lastModified() > destination.lastModified()`, and a destination that
    does not exist yet reports 0.  Source also 0, so `0 > 0` is false: the plugin
    creates the output directories, logs `Copying 76 resources`, copies nothing,
    and the build goes green.  Filtered resources are unaffected, because
    interpolation has to rewrite them regardless -- which is why core ends up with
    exactly the three files its filtered <resource> block names (version,
    builddate, gitinfo) and none of the other 76.

    Measured: the same tarball extracted with mtimes gives core/target/classes 3
    files and 0 translation .txt; extracted without them, 79 and 49.  The 49 are
    com/graphhopper/util/*.txt, and TranslationMap.doImport reads them off the
    classpath at startup, so without them a tree builds, shades a 45 MiB jar,
    throws `IllegalStateException: No input stream found in class path!?` from
    every rung of the launch ladder, and never binds a port -- or worse, starts and
    answers a German route in English.  Neither failure names a timestamp, and both
    look like the tree's own fault.

    A fixed instant rather than `now`, so two builds of the same tree are the same
    build; the value matches the environment image's.  `-h` stamps a symlink rather
    than its target, because a submission may contain a link that points nowhere
    and following it would fail on a file that has nothing to do with the build.
    `find`'s exit status is not trusted for the same reason -- one unstampable path
    should not take the build down -- so the result is verified directly instead,
    and a tree that still holds epoch-0 files raises rather than returning, since
    the alternative is a green build packaging a jar with no resources in it.
    """
    before = [p for p in tree.rglob("*")
              if not p.is_symlink() and p.stat().st_mtime == 0]
    subprocess.run(["find", str(tree), "-exec", "touch", "-h", "-d",
                    _FIXED_MTIME, "{}", "+"],
                   check=False, capture_output=True)
    stale = [p for p in tree.rglob("*")
             if not p.is_symlink() and p.stat().st_mtime == 0]
    if stale:
        raise RuntimeError(
            f"{len(stale)} path(s) under {tree} still have mtime 0 after the "
            f"re-stamp, e.g. {[str(p) for p in stale[:5]]}. "
            "maven-resources-plugin will skip copying them and the jar will be "
            "packaged without its resources.")
    return len(before)


def build(tree: Path, log_path: Path, goals: list[str] | None = None,
          timeout: float = 3600.0) -> str:
    """Run Maven offline in `tree`, writing the full log to `log_path`.

    Offline is not a convenience here.  The stage runs with no network, and a
    build that reached one would resolve whatever the local repository is missing
    — turning an image bug into a silent difference between the two sides.

    Note what the local repository in THIS image does and does not contain, since
    it is the opposite of the agent's.  The agent's repository has the retired
    framework's jars removed, and that omission is how the retirement is enforced
    while the work is being done.  This image keeps the complete closure, because
    the reference built here IS State A and its whole web layer is written against
    that framework.  So "offline" here means "resolve from the union of both sides'
    needs", and the retirement is not re-enforced by this stage at all — stage 1
    judges it, and this stage only asks whether the two behave alike.
    """
    # `--settings` is not optional.  The offline mirror must be the one named
    # `central`, because Maven records in `_remote.repositories` which repository
    # each artefact came from and refuses, offline, to use an artefact whose
    # provenance names a repository it is not currently configured with.  Without
    # this file every dependency in the tree becomes unresolvable, with an error
    # that talks about the artefact instead of about the mirror.
    #
    # `-ntp` because transfer progress on a warm local repository is thousands of
    # lines of nothing, and the log is what a failing build is diagnosed from.
    cmd = ([MVN, "-B", "-ntp", "--settings", _SETTINGS, "-o", "-DskipTests"]
           + (goals or ["package"]))
    # `submission_env`, not a copy of this process's environment: the build being
    # launched is the submission's own, and the eight SRB_* contract names would
    # hand it the tests directory, the reference tree and the result path.
    #
    # This said "Stage 3 calls this same function, so both stages hand the identical
    # environment to the identical build".  No stage calls another stage's copy of
    # it.  This file exists twice -- `tests/behavioural/lib/harness/maven.py` and
    # `tests/verification/lib/harness/maven.py` -- kept in step by hand, and when the
    # stage-2 copy adopted `submission_env` the stage-3 copy went on building with
    # `dict(os.environ)`.  So the verification stage, where a submission reading the
    # grader's own paths is precisely what is being probed for, was the one stage
    # handing them over, and every sha256 pin passed while it did.  Both copies do
    # this now.  Neither says the stages share code, because nothing in either file
    # can enforce that and a comment that reads as a guarantee is worse than one
    # that reads as a note; `infra/tests/test_layout.py` is what holds it.
    #
    # Written from neither path on purpose.  The two copies must stay byte-identical,
    # so a sentence like "stage 3 has its own copy under tests/verification/" is true
    # in one of them and, read in the other, describes the file it is written in as
    # somebody else's.
    env = submission_env()
    env["MAVEN_OPTS"] = (f"-Dmaven.repo.local={_M2_REPO} -Xmx2g "
                         "-Djava.awt.headless=true")
    # A submitted tree's own JAVA_TOOL_OPTIONS would apply to Maven itself and to
    # every test JVM it forks; MAVEN_ARGS would let it inject flags into a command
    # this harness composed deliberately.
    env.pop("JAVA_TOOL_OPTIONS", None)
    env.pop("MAVEN_ARGS", None)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "wb") as sink:
        proc = subprocess.run(cmd, cwd=str(tree), stdout=sink,
                              stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, env=env,
                              timeout=timeout, check=False)
    log = log_path.read_text(errors="replace")
    if proc.returncode != 0:
        raise BuildFailed(
            f"`{' '.join(cmd)}` exited {proc.returncode} in {tree}", log)
    return log


def _is_executable_jar(path: Path) -> tuple[bool, str]:
    """Whether a jar can be started with `java -jar`, and what makes it so."""
    name = path.name
    if any(name.endswith(s) for s in _BYPRODUCT_SUFFIXES):
        return False, "build by-product"
    if any(name.startswith(p) for p in _BYPRODUCT_PREFIXES):
        return False, "build by-product"
    try:
        with zipfile.ZipFile(path) as z:
            try:
                manifest = z.read("META-INF/MANIFEST.MF").decode(
                    "utf-8", errors="replace")
            except KeyError:
                return False, "no manifest"
            # Spring Boot's launcher sets Main-Class to its own launcher and
            # records the application's entry point in Start-Class; a shaded
            # Dropwizard jar sets Main-Class directly.  Either is launchable.
            for line in manifest.splitlines():
                if line.startswith("Main-Class:"):
                    return True, line.strip()
        return False, "manifest declares no Main-Class"
    except (zipfile.BadZipFile, OSError) as exc:
        return False, f"unreadable: {exc}"


def find_app_jar(tree: Path) -> tuple[Path, list[str]]:
    """The runnable jar the build produced, plus the reasoning as notes.

    Raises RuntimeError naming every jar it considered when none qualifies —
    a submission whose build "succeeded" but produced nothing launchable should
    be told which files were examined, not just that the search failed.
    """
    candidates = []
    notes = []
    for jar in sorted(tree.rglob("target/*.jar")):
        ok, reason = _is_executable_jar(jar)
        rel = jar.relative_to(tree)
        if ok:
            candidates.append(jar)
            notes.append(f"candidate {rel} ({jar.stat().st_size // 1024} KiB): {reason}")
        else:
            notes.append(f"skipped {rel}: {reason}")
    if not candidates:
        raise RuntimeError(
            "the build produced no jar that `java -jar` could start.\n  "
            + "\n  ".join(notes or ["no jars found under any target/ directory"]))
    best = max(candidates, key=lambda p: p.stat().st_size)
    notes.append(f"selected {best.relative_to(tree)} (largest launchable jar)")
    return best, notes


def entry_point(path: Path) -> str:
    """What `java -jar` on this jar will actually run.

    Spring Boot's repackage sets `Main-Class` to its own launcher and records the
    application in `Start-Class`; a shaded jar sets `Main-Class` directly.  Both are
    read, because which class runs is what distinguishes an application jar from a
    command-line tool that merely happens to be executable — and the rule in
    `find_app_jar` cannot tell them apart, so the report has to say which it got.
    """
    try:
        with zipfile.ZipFile(path) as z:
            manifest = z.read("META-INF/MANIFEST.MF").decode(
                "utf-8", errors="replace")
    except (KeyError, zipfile.BadZipFile, OSError):
        return ""
    main = start = ""
    for line in manifest.splitlines():
        if line.startswith("Main-Class:"):
            main = line.split(":", 1)[1].strip()
        elif line.startswith("Start-Class:"):
            start = line.split(":", 1)[1].strip()
    return f"{start} (via {main})" if start and main else (main or start)


def module_dir(tree: Path, jar: Path) -> str:
    """The reactor module a jar came out of: whatever directory holds its `target/`."""
    parts = jar.relative_to(tree).parts
    if "target" in parts:
        head = parts[:parts.index("target")]
        return "/".join(head) if head else "."
    return str(jar.relative_to(tree).parent)


def module_jars(tree: Path, module: str) -> list[tuple[str, bool, str]]:
    """Every jar one module produced, with whether `java -jar` could start it.

    Used to say *why* the selection landed outside the module the other side's app
    jar came from: the interesting case is that the module still exists and built,
    and its jar simply carries no `Main-Class`.
    """
    base = tree if module == "." else tree / module
    out = []
    for jar in sorted((base / "target").glob("*.jar")):
        ok, reason = _is_executable_jar(jar)
        out.append((str(jar.relative_to(tree)), ok, reason))
    return out


def module_list(tree: Path, log: str) -> list[str]:
    """The reactor's module list, read from a build log.

    From the log rather than by parsing poms: the log records what Maven actually
    built, and a pom parser would have to reimplement profile activation to agree
    with it.

    Maven puts a blank `[INFO]` line between the header and the first module::

        [INFO] Reactor Build Order:
        [INFO]
        [INFO] GraphHopper Parent Project                            [pom]
        ...
        [INFO] GraphHopper Example                                   [jar]
        [INFO]
        [INFO] -----------< com.graphhopper:graphhopper-parent >-----------

    That blank does both jobs -- it separates the header from the list and it
    closes the list -- so a break on emptiness has to be gated on having
    collected a module.  Ungated it returns nothing, and nothing is the one
    result that cannot be read as wrong: `run.py:185` formats
    `f"{len(mods)} reactor module(s)"` for each side, so an empty list has both
    reporting the same count with an empty detail, and the comparison the module
    exists to make says nothing at all.

    Two packaging forms are handled.  `[jar]` bracketed is what Maven 3.x emits
    and what the bracket branch trims; the bare trailing word is the older
    spelling, cheap to strip and harmless where it does not occur.

    Only the behavioural stage calls this -- `modules/build/run.py`; `lib/stage3.py`
    uses `scrub_build_output`, `normalise_mtimes`, `build` and `find_app_jar` and
    nothing else -- so the verification copy of this file never reaches it.  It is
    kept rather than trimmed to that stage's needs because
    `infra/tests/test_layout.py` holds the two copies byte-identical, and a
    harness function that is right in one copy and wrong in the other is how the
    next port inherits the wrong one.
    """
    mods: list[str] = []
    started = False
    for line in log.splitlines():
        if "Reactor Build Order:" in line:
            started = True
            continue
        if not started:
            continue
        stripped = line.replace("[INFO]", "").strip()
        if not stripped:
            if mods:
                break       # the blank that closes the list
            continue        # the blank that follows the header
        if stripped.startswith("-"):
            break
        if stripped.endswith("]") and "[" in stripped:
            stripped = stripped[:stripped.rindex("[")].strip()
        elif stripped.endswith(("jar", "pom", "war")):
            stripped = stripped.rsplit(" ", 1)[0].strip()
        mods.append(stripped)
    return mods
