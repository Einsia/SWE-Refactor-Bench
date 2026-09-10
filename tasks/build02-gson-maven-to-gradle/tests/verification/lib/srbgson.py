#!/usr/bin/env python3
"""Build the tree in front of you, and ask the artifacts questions.

This is the whole capability surface of stage 3.  A candidate imports it, names a
configuration, and gets back jars, a published repository, a test run, and the
ability to compile a consumer program against what the build produced and run it.

WHAT IT DELIBERATELY DOES NOT GIVE YOU
--------------------------------------
A path to the tree under test.  What is graded is what the build ships, and a
candidate that reads the repository is asserting on the build system's internal
organisation -- which stage 1 already reviewed and which this stage's deny list
rejects.  So there is no `Tree.root`, no `Tree.source`, and no export naming
either tree.

The name of the build system.  Not because it is a secret -- one tree has a
`pom.xml` and the other has a `settings.gradle`, and no amount of hiding changes
that -- but because a candidate that branches on it has stopped measuring the
migration.  Such a candidate satisfies every mechanical condition for a break and
establishes nothing, and it is rejected on sight rather than weighed.

THE ONE THING THAT MAKES THIS TASK DIFFERENT FROM THE OTHER BUILD TASKS
----------------------------------------------------------------------
There is no single command that builds both trees.  A Maven reactor and a Gradle
multi-project are asked for the same thing in different words, so unlike a task
where both sides are PEP 517 packages, the translation has to live somewhere.  It
lives here, in `_GOALS`, written once per configuration:

    default    mvn clean package -DskipTests   |  gradle clean assemble
    full       mvn clean package               |  gradle clean build
    publish    mvn clean deploy -Dalt...       |  gradle clean publish -PsrbPublishDir=...

That is the harness's translation, not the submission's, and it is fixed.  A
candidate never spells a goal or a task name, cannot add one, and therefore cannot
construct a request that one dialect can express and the other cannot.  The
consequence worth stating: a difference you find is a difference in what these two
requests PRODUCED, never a difference in how they were spelled.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import classfile
import jarinspect

__all__ = [
    "CONFIGURATIONS",
    "PROJECTS",
    "BuildFailed",
    "Output",
    "TestRun",
    "Tree",
    "class_bytes_major",
    "class_module",
    "class_strings",
    "configurations",
    "jar_of",
    "tree",
    "zip_entries",
    "zip_read",
]

# --------------------------------------------------------------------------- #
# Where things are
# --------------------------------------------------------------------------- #
# $SRB_TARGET is the tree, and it is read here and never exported.  The token is
# what names anything a candidate can see, so a directory listing under $SRB_STATE
# says "3f9c1a..." rather than "submission".
_TARGET = Path(os.environ.get("SRB_TARGET", "/nonexistent"))
_TOKEN = os.environ.get("SRB_TARGET_TOKEN", "unknown")
_STATE = Path(os.environ.get("SRB_STATE", "/tmp/srb-adv-state"))

#: The offline closure both dialects resolve against.  No build may write the
#: repository under test into it; see `_goals` and `_assert_closure_clean`.
_SHARED_M2 = Path(os.environ.get("SRB_SHARED_M2", "/root/.m2/repository"))


def _gson_versions_in_closure() -> set[str]:
    """`artifact/version` for every com.google.code.gson module in the closure."""
    group = _SHARED_M2 / "com" / "google" / "code" / "gson"
    if not group.is_dir():
        return set()
    return {"%s/%s" % (p.parent.name, p.name)
            for p in group.glob("*/*") if p.is_dir()}


#: What the closure legitimately holds, read before any build runs.  gson 2.8.5 and
#: 2.8.7 are in there as dependencies of one of the plugins; anything that appears
#: later was put there by a build, which is what must not happen.
_CLOSURE_GSON = _gson_versions_in_closure()
_SCRATCH = Path(os.environ.get("SRB_SCRATCH", "/tmp/srb-adv-scratch"))

#: run-candidate.sh writes the dialect here, from its own positional argument.
#: Reading it tells a candidate which tree it is on, and reading it is worth
#: nothing: see the module docstring.
_DIALECT_FILE = _STATE / "dialect"

_MVN = os.environ.get("SRB_MVN", "mvn")
_GRADLE = os.environ.get("SRB_GRADLE", "gradle")
_SETTINGS = os.environ.get("SRB_MVN_SETTINGS", "/root/.m2/settings.xml")

#: The four projects, by the directory name both trees use.  Keyed this way on
#: purpose: `gson-2.10.1.jar` and `gson-proto.jar` are naming conventions, and
#: keying by one build's filenames would make that build the reference.
PROJECTS = ("gson", "extras", "metrics", "proto")

#: What a candidate may ask for.  A name not in here raises rather than silently
#: building something else.
CONFIGURATIONS = ("default", "full", "publish", "relocated")

_PUBLISH_DIR = "srb-publish"
_PUBLISH_PROPERTY = "srbPublishDir"
_RELOCATED_SUBDIR = "a/deeper/place/gson-2.10.1"

_DEFAULT_TIMEOUT = float(os.environ.get("SRB_BUILD_TIMEOUT", "2400"))
_RUN_TIMEOUT = float(os.environ.get("SRB_RUN_TIMEOUT", "300"))


class BuildFailed(RuntimeError):
    """The tree would not build in this configuration.

    Not a finding on its own: a submission that does not build is stage 2's
    verdict.  Carries the tail of the log so a candidate can tell "the build is
    broken" from "my mutation does not compile", which are different mistakes.
    """

    def __init__(self, config: str, status: int, log: str) -> None:
        self.config = config
        self.status = status
        self.log = log
        super().__init__(
            f"the build failed in configuration {config!r} (exit {status}). "
            f"Log tail:\n{log[-4000:]}"
        )


# --------------------------------------------------------------------------- #
# The translation, in one place
# --------------------------------------------------------------------------- #
def _deploy_settings() -> Path:
    """The image's settings.xml with `<offline>` dropped, written once.

    maven-deploy-plugin refuses to run when Maven is offline -- "Cannot deploy
    artifacts when Maven is in offline mode" -- and it checks that before it looks at
    where it was told to deploy, so deploying to a `file://` repository is refused
    too.  This is the only way to run the goal at all.

    It opens nothing.  The container has no network, which is what actually enforces
    isolation here, and the mirror this file points Central at is unreachable, so a
    resolution that escaped the closure still fails loudly instead of succeeding.
    The flag only ever stopped the one goal.

    Derived from the real file rather than shipped as a second copy, so the two
    cannot drift.
    """
    out = _STATE / "settings-deploy.xml"
    if not out.exists():
        text = Path(_SETTINGS).read_text(encoding="utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(re.sub(r"[ \t]*<offline>.*?</offline>[ \t]*\r?\n?", "",
                             text), encoding="utf-8")
    return out


#: The reactor root.  A Maven `deploy` publishes the parent POM because a consumer
#: resolving gson's POM has to fetch it to resolve the POM at all; a Gradle build has
#: no parent POM and publishes none, and the behavioural stage requires that the
#: submission publish neither it nor a `<parent>` referring to it.  So its presence
#: on one side is specified, not a defect, and `published()` leaves it out.
_PARENT_ARTIFACT = "gson-parent"


def _is_publisher_bookkeeping(rel: str) -> bool:
    """True for a path that records how a publication was made, not what it is.

    `deploy` writes `maven-metadata.xml` and md5/sha1 sidecars beside everything,
    `install` writes `_remote.repositories` and `maven-metadata-local.xml`, and
    Gradle's maven-publish writes none of those but does write Gradle Module Metadata
    and sha256/sha512.  Not one of them is the publication; each is a property of the
    publisher.  Left in, the two dialects would differ here for every candidate, in a
    way that says nothing about whether the migration is faithful -- a false finding,
    and the expensive kind, because it looks like a real one.

    The behavioural stage takes the same position where it checks the published file
    set: the required artifacts are fixed, the sidecars are not.
    """
    name = rel.replace(os.sep, "/").rsplit("/", 1)[-1]
    if ("/%s/" % _PARENT_ARTIFACT) in "/" + rel.replace(os.sep, "/"):
        return True
    return (name == "_remote.repositories"
            or name.startswith("maven-metadata")
            or name.endswith((".module", ".md5", ".sha1", ".sha256", ".sha512",
                              ".asc")))


def _goals(dialect: str, config: str, publish_dir: Path | None) -> list[str]:
    if dialect == "maven":
        base = [_MVN, "-B", "-o", "--settings", _SETTINGS]
        if config == "full":
            return base + ["clean", "package"]
        if config == "publish":
            # `deploy` to a file:// repository of its own, with install skipped.
            #
            # Three things are being avoided here, and each of them was tried the
            # other way first.
            #
            # Not into ~/.m2/repository.  That is the offline closure both dialects
            # resolve against.  A real com.google.code.gson:gson:2.10.1 left there
            # is resolvable, the Gradle init script gives every project
            # mavenLocal() as its only repository, and a submission that declared
            # an external dependency on gson instead of a project dependency --
            # which is a defect, and one stage 2 gates on -- would then resolve it
            # and build clean.  Which tree a candidate built first would decide it.
            # Hence `maven.install.skip`: deploy is a later phase than install, so
            # running deploy runs install too unless it is turned off.
            #
            # `deploy` and not `install`, even though install needs no settings
            # change.  extras, metrics and proto each set maven-deploy-plugin's
            # <skip>true</skip>, so `deploy` publishes gson alone -- which is what
            # the Gradle side publishes, and what the behavioural stage requires of
            # it.  `install` ignores that configuration and installs all four, and
            # a candidate would then see four artifacts on one side and one on the
            # other: a false finding, and the convincing kind.
            #
            # And a settings file with `<offline>` dropped, because the goal is
            # refused outright otherwise.  See `_deploy_settings`.
            return [_MVN, "-B", "--settings", str(_deploy_settings()),
                    "clean", "deploy", "-DskipTests",
                    "-Dmaven.install.skip=true",
                    "-DaltDeploymentRepository=srb::default::file://%s"
                    % (publish_dir or Path("/tmp/srb-publish"))]
        return base + ["clean", "package", "-DskipTests"]
    if dialect == "gradle":
        base = [_GRADLE, "--offline", "--no-daemon", "--console=plain",
                "--stacktrace"]
        if config == "full":
            return base + ["clean", "build"]
        if config == "publish":
            return base + ["clean", "publish",
                           "-P%s=%s" % (_PUBLISH_PROPERTY,
                                        publish_dir or Path("/tmp/srb-publish"))]
        return base + ["clean", "assemble"]
    raise ValueError("unknown dialect %r" % dialect)


def _dialect() -> str:
    try:
        value = _DIALECT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if value not in ("maven", "gradle"):
        raise RuntimeError(
            "the stage did not record which dialect to use for this tree "
            f"({_DIALECT_FILE}). This is a harness fault, not a finding."
        )
    return value


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment every build runs in.

    Nothing is shadowed here, and that is the opposite of what stage 2 does.  Stage
    2 puts a recording stub for `mvn` and fourteen other engine names first on PATH,
    because its whole claim is that the migrated tree does not need them.  This
    stage must not: its job is to compare what two WORKING builds produced, and a
    submission that cannot build here because something it needed was shadowed
    would make every candidate pass on the original and fail on the submission --
    six breaks, and a zero for a bug of ours.

    Whether the submission still needs Maven has already been decided by stage 2,
    where it is measured.  Re-deciding it here, in a stage that pays per round,
    would charge for it twice.
    """
    env = dict(os.environ)
    env["LC_ALL"] = "C.UTF-8"
    env["LANG"] = "C.UTF-8"
    env["TZ"] = "UTC"
    env["TERM"] = "dumb"
    env["HOME"] = os.environ.get("HOME", "/root")
    env.setdefault("GRADLE_USER_HOME", "/root/.gradle")
    env["MAVEN_OPTS"] = "-Dmaven.repo.local=/root/.m2/repository"
    # Nothing a candidate's own process is holding may reach a build.  A build
    # that only succeeds because JAVA_TOOL_OPTIONS was set is not the build, and
    # the two trees would not be getting the same treatment.
    for leaked in ("GRADLE_OPTS", "MAVEN_ARGS", "JAVA_TOOL_OPTIONS",
                   "_JAVA_OPTIONS", "ANT_HOME", "ANT_OPTS", "M2_HOME",
                   "CLASSPATH", "SRB_TARGET", "SRB_TARGET_TOKEN", "SRB_STATE",
                   "SRB_TARGET_ROLE", "SRB_TARGET_NAME", "SRB_ORIGINAL"):
        env.pop(leaked, None)
    if extra:
        env.update(extra)
    return env


# --------------------------------------------------------------------------- #
# Running things
# --------------------------------------------------------------------------- #
class Output:
    """One command's result.

    `stdout` and `stderr` are bytes.  A JSON document written by a probe program
    and a class file read out of a jar are both byte strings, and decoding at the
    boundary is how a real difference in encoding gets hidden.  `text()` decodes
    when a str is what you want.

    Returned rather than raised on a non-zero exit: "runs against one tree and
    crashes against the other" is exactly a finding, so it must be observable
    rather than exceptional.
    """

    def __init__(self, argv, returncode, stdout, stderr, duration,
                 timed_out=False):
        self.argv = list(argv)
        self.returncode = returncode
        self.stdout = stdout or b""
        self.stderr = stderr or b""
        self.duration_sec = duration
        self.timed_out = timed_out

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def text(self, encoding: str = "utf-8") -> str:
        return self.stdout.decode(encoding, "replace")

    def lines(self, encoding: str = "utf-8") -> list[str]:
        return [ln for ln in self.text(encoding).splitlines() if ln.strip()]

    def json(self):
        return json.loads(self.stdout.decode("utf-8"))

    def __str__(self) -> str:
        return (
            "argv: %s\nexit: %s%s (%.1fs)\n--- stdout ---\n%s\n--- stderr ---\n%s"
            % (" ".join(self.argv), self.returncode,
               " TIMED OUT" if self.timed_out else "", self.duration_sec,
               self.stdout.decode("utf-8", "replace")[-4000:],
               self.stderr.decode("utf-8", "replace")[-4000:])
        )

    __repr__ = __str__


def _run(argv, cwd, env, timeout) -> Output:
    start = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=str(cwd), env=env, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired as exc:
        return Output(argv, -1, exc.stdout, exc.stderr,
                      time.monotonic() - start, timed_out=True)
    except OSError as exc:
        return Output(argv, -1, b"", str(exc).encode(),
                      time.monotonic() - start)
    return Output(argv, proc.returncode, proc.stdout, proc.stderr,
                  time.monotonic() - start)


# --------------------------------------------------------------------------- #
# Test results, in whichever place the build wrote them
# --------------------------------------------------------------------------- #
class TestRun:
    """What the build's own test task reported, read from its JUnit XML.

    Both dialects write JUnit XML; they write it in different directories, and
    finding it is the harness's job rather than a candidate's.  What comes back is
    the same shape either way -- counts and a set of test ids -- which is what
    makes "the original runs this case and the submission does not" a question a
    candidate can ask in one line.

    Gson's own suite is the largest instrument in this stage: a case that passes
    against one build and fails or vanishes against the other is the strongest
    finding available here, because it is the project's own statement about its own
    behaviour rather than anything the benchmark made up.
    """

    #: Where each dialect leaves its XML, relative to a project directory.
    _DIRS = ("target/surefire-reports",           # Maven
             "build/test-results/test",           # Gradle
             "build/test-results",                # Gradle, older layouts
             "target/test-results")

    def __init__(self, per_project: dict[str, dict]):
        self.per_project = per_project

    @property
    def projects(self) -> tuple[str, ...]:
        return tuple(sorted(self.per_project))

    def _sum(self, key: str) -> int:
        return sum(v[key] for v in self.per_project.values())

    @property
    def total(self) -> int:
        return self._sum("tests")

    @property
    def failures(self) -> int:
        return self._sum("failures")

    @property
    def errors(self) -> int:
        return self._sum("errors")

    @property
    def skipped(self) -> int:
        return self._sum("skipped")

    @property
    def passed(self) -> int:
        return self.total - self.failures - self.errors - self.skipped

    @property
    def all_passed(self) -> bool:
        return self.total > 0 and self.failures == 0 and self.errors == 0

    def counts(self, project: str | None = None) -> dict[str, int]:
        if project is not None:
            v = self.per_project.get(project)
            if v is None:
                return {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
            return {k: v[k] for k in ("tests", "failures", "errors", "skipped")}
        return {"tests": self.total, "failures": self.failures,
                "errors": self.errors, "skipped": self.skipped}

    def classes(self, project: str | None = None) -> tuple[str, ...]:
        """Every test class the run reported, as `com.google.gson.FooTest`."""
        out: set[str] = set()
        for name, v in self.per_project.items():
            if project in (None, name):
                out.update(v["classes"])
        return tuple(sorted(out))

    def cases(self, project: str | None = None) -> tuple[str, ...]:
        """Every case, as `com.google.gson.FooTest#bar`."""
        out: set[str] = set()
        for name, v in self.per_project.items():
            if project in (None, name):
                out.update(v["cases"])
        return tuple(sorted(out))

    def outcome(self, case: str) -> str:
        """`pass` | `fail` | `error` | `skip` for one case id, or `absent`."""
        for v in self.per_project.values():
            if case in v["outcomes"]:
                return v["outcomes"][case]
        return "absent"

    def __str__(self) -> str:
        parts = ["%s: %d tests, %d failures, %d errors, %d skipped"
                 % (p, v["tests"], v["failures"], v["errors"], v["skipped"])
                 for p, v in sorted(self.per_project.items())]
        return ("test run -- %d cases over %d project(s)\n  %s"
                % (self.total, len(self.per_project), "\n  ".join(parts)))

    __repr__ = __str__


def _read_junit(root: Path) -> dict[str, dict]:
    per: dict[str, dict] = {}
    for project in PROJECTS:
        base = root / project
        if not base.is_dir():
            continue
        found: list[Path] = []
        for rel in TestRun._DIRS:
            d = base / rel
            if d.is_dir():
                found += sorted(p for p in d.glob("*.xml")
                                if p.name.startswith("TEST-")
                                or p.name.endswith(".xml"))
        if not found:
            continue
        agg = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0,
               "classes": set(), "cases": set(), "outcomes": {}}
        for xml in found:
            _absorb_junit(xml, agg)
        if agg["tests"] or agg["cases"]:
            agg["classes"] = tuple(sorted(agg["classes"]))
            agg["cases"] = tuple(sorted(agg["cases"]))
            per[project] = agg
    return per


_SUITE_RE = re.compile(rb"<testsuite\b[^>]*>")
_CASE_RE = re.compile(rb"<testcase\b(.*?)(/>|>)", re.S)
_ATTR_RE = re.compile(rb'([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*"([^"]*)"')


def _attrs(blob: bytes) -> dict[str, str]:
    return {k.decode("utf-8", "replace"): v.decode("utf-8", "replace")
            for k, v in _ATTR_RE.findall(blob)}


def _absorb_junit(path: Path, agg: dict) -> None:
    """Read one JUnit XML file without an XML parser's failure modes.

    Deliberately regex rather than ElementTree: a surefire report can carry raw
    control characters inside a `<system-out>` from a test that printed them, and
    a strict parser raises on the whole file.  Losing a project's entire test
    report to one unprintable byte would look like "the build ran no tests", which
    is a finding-shaped lie.  The attributes read here are machine-written and
    quoted, so the pattern is safe on the part that matters.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return
    for suite in _SUITE_RE.findall(raw):
        a = _attrs(suite)
        for key, attr in (("tests", "tests"), ("failures", "failures"),
                          ("errors", "errors")):
            try:
                agg[key] += int(a.get(attr, "0") or 0)
            except ValueError:
                pass
        for attr in ("skipped", "disabled"):
            try:
                agg["skipped"] += int(a.get(attr, "0") or 0)
            except ValueError:
                pass
    for body, closing in _CASE_RE.findall(raw):
        a = _attrs(body)
        cls = a.get("classname") or a.get("class") or ""
        name = a.get("name") or ""
        if not cls and not name:
            continue
        if cls:
            agg["classes"].add(cls)
        case = "%s#%s" % (cls, name) if cls else name
        agg["cases"].add(case)
        # `<testcase .../>` is a pass.  Anything with a body may carry a
        # <failure>, <error> or <skipped>, and the body is whatever follows up to
        # the matching close -- which the non-greedy pattern above does not give
        # us, so look at the file around this case instead.
        verdict = "pass"
        if closing == b">":
            start = raw.find(body)
            window = raw[start:start + 20000]
            end = window.find(b"</testcase>")
            if end >= 0:
                window = window[:end]
            if b"<failure" in window:
                verdict = "fail"
            elif b"<error" in window:
                verdict = "error"
            elif b"<skipped" in window:
                verdict = "skip"
        agg["outcomes"][case] = verdict


# --------------------------------------------------------------------------- #
# A built tree
# --------------------------------------------------------------------------- #
class Tree:
    """One configuration, built.  Everything here addresses the ARTIFACTS."""

    def __init__(self, config: str, root: Path, log: Path, dialect: str,
                 publish_dir: Path | None, source_root: Path):
        self._config = config
        self._root = root
        self._dialect = dialect
        self._publish = publish_dir
        self._source = source_root
        self.log = log
        self._jars: dict[str, Path] | None = None
        self._open: dict[str, jarinspect.Jar] = {}
        self._probes = 0

    # -- the jars ---------------------------------------------------------- #
    @property
    def config(self) -> str:
        return self._config

    def jars(self) -> dict[str, Path]:
        """project directory -> the one jar that project's build produced.

        Sidecars are excluded: a `-sources.jar` or a `-javadoc.jar` is not the
        project's artifact, and a build that produced only sidecars has produced
        no jar rather than four.
        """
        if self._jars is None:
            found: dict[str, Path] = {}
            for project in PROJECTS:
                for rel in ("target", "build/libs"):
                    d = self._root / project / rel
                    if not d.is_dir():
                        continue
                    candidates = [p for p in sorted(d.glob("*.jar"))
                                  if not _is_sidecar(p.name)]
                    if candidates:
                        found[project] = candidates[0]
                        break
            self._jars = found
        return dict(self._jars)

    def jar(self, project: str) -> jarinspect.Jar:
        """The project's jar, open for reading.  Raises if the build made none."""
        if project in self._open:
            return self._open[project]
        path = self.jars().get(project)
        if path is None:
            raise FileNotFoundError(
                "configuration %r produced no jar for the %r project (jars: %s)"
                % (self._config, project, sorted(self.jars())))
        jar = jarinspect.Jar(path)
        self._open[project] = jar
        return jar

    def jar_name(self, project: str) -> str | None:
        p = self.jars().get(project)
        return p.name if p else None

    def entries(self, project: str) -> tuple[str, ...]:
        # `Jar.entries` and `Jar.manifest` are properties, not methods.  These two
        # wrappers exist so a candidate does not have to know which of the reader's
        # members are which, and so `t.entries(...)` reads the same as every other
        # Tree accessor.
        return tuple(self.jar(project).entries)

    def read(self, project: str, entry: str) -> bytes:
        return self.jar(project).read(entry)

    def manifest(self, project: str) -> dict[str, str]:
        return dict(self.jar(project).manifest)

    def sha256(self, project: str, entry: str) -> str:
        return self.jar(project).sha256(entry)

    def majors(self, project: str) -> dict[str, int]:
        """entry -> class-file major version, for every class in the jar."""
        return self.jar(project).majors()

    # -- the publication --------------------------------------------------- #
    def published(self) -> dict[str, Path]:
        """repository-relative path -> file, for the `publish` configuration.

        This is what a consumer resolving `com.google.code.gson:gson:2.10.1` would
        fetch: the artifacts, and nothing that merely records how they got there.
        Checksum sidecars, signatures, repository metadata, Gradle Module Metadata
        and the reactor's parent POM are left out, because each is written by one
        dialect and not the other -- so comparing the raw directories would show a
        difference for every candidate that says nothing about whether the migration
        is faithful.  See `_is_publisher_bookkeeping`.

        Empty for every configuration but `publish`.
        """
        if self._publish is None or not self._publish.is_dir():
            return {}
        out: dict[str, Path] = {}
        for p in sorted(self._publish.rglob("*")):
            rel = str(p.relative_to(self._publish))
            if p.is_file() and not _is_publisher_bookkeeping(rel):
                out[rel] = p
        return out

    def published_pom(self, artifact: str, version: str = "2.10.1"):
        """The published POM for one artifact id, as a `Pom`, or None.

        Carries the repository root, so `Pom.coordinates`, `Pom.inherited` and
        `Pom.dependencies` resolve through a `<parent>` if there is one.  That
        matters for exactly the reason the prompt warns about: one dialect writes a
        thin POM beside its parent and the other writes a flat one, so a field read
        off the file is present on one side and absent on the other for no reason
        that involves either repository.  Read through the chain, both sides agree.

        The parent itself is filtered out of `published()` as bookkeeping, but it is
        still on disk, which is where the chain finds it -- a consumer resolving
        this POM would fetch it the same way.
        """
        want = "com/google/code/gson/%s/%s/%s-%s.pom" % (
            artifact, version, artifact, version)
        for rel, path in self.published().items():
            if rel.replace(os.sep, "/") == want:
                return jarinspect.Pom(path, repo_root=self._publish)
        return None

    # -- the tests --------------------------------------------------------- #
    def tests(self) -> TestRun:
        """What the build's own test task reported.

        Empty unless the configuration ran tests -- `full` does, `default` does
        not.  A `TestRun` with `total == 0` means the tests did not run, which is
        not the same as the tests passing, so check `total` before believing
        `all_passed`.
        """
        return TestRun(_read_junit(self._root))

    # -- using what was built ---------------------------------------------- #
    def java(self, source: str, *args: str, classpath: str = "",
             module_path: str = "", modules: str = "",
             main: str | None = None, stdin: bytes | None = None,
             timeout: float | None = None) -> Output:
        """Compile a consumer program against this build's jars and run it.

        This is the instrument the rest of the module exists to support, and the
        analogue of linking a C program against an installed library: the jars go
        on the classpath, your source is compiled by the image's javac and run by
        the image's java, and what it prints is the observation.

            out = t.java('''
                public class Probe {
                  public static void main(String[] a) {
                    System.out.println(new com.google.gson.Gson()
                        .toJson(new int[] {1, 2, 3}));
                  }
                }''')
            assert out.ok and out.text().strip() == "[1,2,3]", out

        The class must be in the default package unless you pass `main`.  Both
        halves are reported rather than raised: a program that compiles against
        one tree and not the other is a finding, and so is one that compiles
        against both and prints different things.

        classpath      extra entries, appended after this build's four jars
        module_path    put on --module-path instead, for a JPMS probe
        modules        --add-modules value
        args           argv for the program
        """
        self._probes += 1
        work = _SCRATCH / ("java-%02d" % self._probes)
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        name = main or _class_name(source) or "Probe"
        src = work / ("%s.java" % name.split(".")[-1])
        src.write_text(source, encoding="utf-8")
        jars = [str(p) for p in self.jars().values()]
        cp = os.pathsep.join([str(work)] + jars + ([classpath] if classpath
                                                   else []))
        env = _clean_env()
        limit = timeout if timeout is not None else _RUN_TIMEOUT

        compile_cp = os.pathsep.join(jars + ([classpath] if classpath else []))
        javac = ["javac", "-nowarn", "-d", str(work)]
        if module_path:
            javac += ["--module-path",
                      os.pathsep.join(jars + [module_path] if module_path
                                      else jars)]
            if modules:
                javac += ["--add-modules", modules]
        if compile_cp:
            javac += ["-classpath", compile_cp]
        javac += [str(src)]
        built = _run(javac, work, env, limit)
        if not built.ok:
            # The compile output is the finding when it is one, so it comes back
            # as the Output rather than as an exception.
            return built

        java = ["java"]
        if module_path:
            java += ["--module-path", os.pathsep.join(jars + [module_path])]
            if modules:
                java += ["--add-modules", modules]
        java += ["-classpath", cp, name, *args]
        return _run_with_stdin(java, work, env, limit, stdin)

    # -- building again, and building something else ----------------------- #
    def build_again(self) -> "Tree":
        """Build this configuration again, from a fresh copy of the sources.

        Reproducibility rather than an incremental rebuild: what this asks is
        whether the build is a function of its inputs.  Not cached -- asking twice
        builds twice, which is the point.  Compare one tree's two builds against
        each other, not one tree against the other.
        """
        return _build(self._config, self._dialect, self._source,
                      slot=_fresh_slot(self._config), mutations=None)

    def with_sources(self, files: dict[str, str | bytes],
                     config: str | None = None) -> "Tree":
        """Build again with these files written into the sources first.

        The sharpest thing in this module, and the only one that asks whether the
        build has RULES.  Every entry name, checksum and manifest header a build
        could be compared on is satisfiable by a build that enumerates what it was
        told to produce.  A source file that did not exist when anything was
        frozen is not: where it lands is decided by the source-set layout, the
        resource wiring, and whatever computes the OSGi exports.

            t2 = t.with_sources({
                "gson/src/main/java/com/google/gson/Zz.java":
                    "package com.google.gson;\\npublic class Zz {}\\n"})
            assert "com/google/gson/Zz.class" in t2.entries("gson")

        A path is repository-relative and must stay inside the tree.  Writing over
        a file that is already there is allowed and is how you ask whether
        something is recompiled rather than copied.

        Note what this is not: a way to edit the build system.  A path under a
        build file -- pom.xml, build.gradle, settings.gradle, gradle.properties
        and their relatives -- is refused, because the two trees do not have the
        same ones and a mutation only one dialect can receive is not a comparison.
        """
        if not files:
            raise ValueError("with_sources was given no files")
        prepared: dict[str, bytes] = {}
        for rel, body in files.items():
            clean = _safe_rel(rel)
            prepared[clean] = (body.encode("utf-8") if isinstance(body, str)
                               else bytes(body))
        return _build(config or self._config, self._dialect, self._source,
                      slot=_fresh_slot(config or self._config),
                      mutations=prepared)

    def __str__(self) -> str:
        return ("tree(%r): %d jar(s) %s, log at %s"
                % (self._config, len(self.jars()),
                   sorted(self.jars()), self.log))

    __repr__ = __str__


_BUILD_FILE_PARTS = (
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "settings.gradle.kts", "gradle.properties", "gradlew", "gradlew.bat",
    "bnd.bnd", "maven-wrapper.properties", "build.xml", "makefile",
    "Makefile", "meson.build", "BUILD", "BUILD.bazel",
)


def _safe_rel(rel: str) -> str:
    p = str(rel).replace("\\", "/").strip("/")
    if not p:
        raise ValueError("with_sources was given an empty path")
    parts = [seg for seg in p.split("/") if seg]
    if any(seg == ".." for seg in parts):
        raise ValueError("with_sources path escapes the tree: %r" % rel)
    if parts[-1] in _BUILD_FILE_PARTS or parts[0] in ("gradle", ".mvn"):
        raise ValueError(
            "with_sources will not write a build file (%r). The two trees do "
            "not have the same build files, so a mutation only one of them can "
            "receive is not a comparison -- mutate SOURCES and ask what each "
            "build did with them." % rel)
    return "/".join(parts)


def _is_sidecar(name: str) -> bool:
    return any(name.endswith(s) for s in
               ("-sources.jar", "-javadoc.jar", "-tests.jar", "-test.jar",
                "-all.jar", "-shaded.jar"))


_CLASS_DECL = re.compile(
    r"^\s*(?:public\s+|final\s+|abstract\s+)*class\s+([A-Za-z_$][\w$]*)",
    re.M)


def _class_name(source: str) -> str | None:
    m = _CLASS_DECL.search(source)
    return m.group(1) if m else None


def _run_with_stdin(argv, cwd, env, timeout, stdin) -> Output:
    start = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=str(cwd), env=env, timeout=timeout,
                              input=stdin, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired as exc:
        return Output(argv, -1, exc.stdout, exc.stderr,
                      time.monotonic() - start, timed_out=True)
    except OSError as exc:
        return Output(argv, -1, b"", str(exc).encode(),
                      time.monotonic() - start)
    return Output(argv, proc.returncode, proc.stdout, proc.stderr,
                  time.monotonic() - start)


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #
_CACHE: dict[str, Tree] = {}
_SLOTS: dict[str, int] = {}


def _fresh_slot(config: str) -> str:
    _SLOTS[config] = _SLOTS.get(config, 0) + 1
    return "%s-%d-%d" % (config, os.getpid(), _SLOTS[config])


def configurations() -> tuple[str, ...]:
    return CONFIGURATIONS


def tree(config: str = "default") -> Tree:
    """Build the tree under test in this configuration, and cache it.

    Configurations are shared across every candidate and every round, so the
    first candidate to ask for one pays for it and the rest are free.  Ask for
    what you need rather than for all of them.

        default     the jars, without running the tests
        full        the jars, with the project's own test suite
        publish     the artifacts written into a file repository, as a consumer
                    would fetch them
        relocated   the same sources built at a deeper absolute path

    Raises `BuildFailed` if the tree will not build that way, which is not a
    finding on its own -- see the class docstring.
    """
    if config not in CONFIGURATIONS:
        raise KeyError("no such configuration %r; have %s"
                       % (config, ", ".join(CONFIGURATIONS)))
    if config in _CACHE:
        return _CACHE[config]
    dialect = _dialect()
    built = _build(config, dialect, _TARGET, slot=config, mutations=None)
    _CACHE[config] = built
    return built


def _build(config: str, dialect: str, source: Path, slot: str,
           mutations: dict[str, bytes] | None) -> Tree:
    base = _STATE / "builds" / slot
    root = base / "src"
    log = base / "build.log"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)

    # `relocated` is the same sources further down: a build file that baked in a
    # path, or that depends on its own depth, answers differently here.
    if config == "relocated":
        root = base / "src" / _RELOCATED_SUBDIR
        root.parent.mkdir(parents=True, exist_ok=True)

    shutil.copytree(source, root, symlinks=True,
                    ignore=shutil.ignore_patterns(".git"))
    _scrub(root)
    if mutations:
        for rel, body in mutations.items():
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)

    publish_dir = (base / _PUBLISH_DIR) if config == "publish" else None
    if publish_dir is not None:
        publish_dir.mkdir(parents=True, exist_ok=True)

    argv = _goals(dialect, config, publish_dir)
    out = _run(argv, root, _clean_env(), _DEFAULT_TIMEOUT)
    log.write_bytes(
        b"$ " + " ".join(argv).encode() + b"\n"
        + b"--- stdout ---\n" + out.stdout
        + b"\n--- stderr ---\n" + out.stderr)
    if not out.ok:
        tail = (out.stdout[-6000:] + b"\n" + out.stderr[-6000:]).decode(
            "utf-8", "replace")
        raise BuildFailed(config, out.returncode, tail)
    if publish_dir is not None:
        _assert_closure_clean(config)
    return Tree(config, root, log, dialect, publish_dir, source)


def _assert_closure_clean(config: str) -> None:
    """The publish build must not have left an artifact in the shared closure.

    Checked rather than assumed.  `maven.install.skip` is one flag, the whole
    separation rests on it, and if a future Maven changed its name the failure would
    be silent: the publication would still look right, and a submission that
    resolved gson externally instead of building it would start passing on the
    second configuration onwards.  Cheap to verify, expensive to miss.
    """
    leaked = sorted(str(p) for p in _gson_versions_in_closure() - _CLOSURE_GSON)
    if leaked:
        raise RuntimeError(
            "the %r build installed artifacts of the repository under test into "
            "the shared offline closure: %s. Every later build in this container "
            "could resolve them instead of compiling, so this is a harness fault "
            "and not a finding." % (config, leaked))


def _scrub(root: Path) -> None:
    """Remove build output the tree arrived carrying.

    The original is unpacked from `original.tar.gz` and is clean by construction.
    The submission is whatever the agent left in `/workspace/repo`, which may hold
    a `target/` from before the migration, a `build/` from the agent's own last
    run, or a jar sitting beside the sources that produced it.  Left in place, a
    stale jar gets picked up as "the jar this build produced" and a candidate
    comparing the two trees is comparing this container's javac against whatever
    built that file.

    Three names, and the shortness is the design.  The near-miss in this task is
    `build`: `build.gradle` is the submission's build system and `build/` is
    Gradle's output directory, so the clause tests `is_dir()` and an exact name and
    the file `build.gradle` is untouched.  A scrub that deleted build.gradle would
    stop the submission from building at all, every candidate would then pass on
    the original and fail on the submission, and six rounds would report six
    breaks -- a zero for a bug of ours.

    Which is why the list is three names and not a pattern.  `out`, `bin` and
    `classes` all fail the same test from the other direction: each is a plausible
    name for delivered content, and removing delivered content produces exactly
    that outcome.  Both trees were checked; neither has a directory by any of those
    names and neither delivers a .jar or a .class.
    """
    for d in sorted(root.rglob("*"), key=lambda p: -len(p.parts)):
        if d.is_dir() and d.name in ("target", "build", ".gradle"):
            shutil.rmtree(d, ignore_errors=True)
    for f in root.rglob("*"):
        if f.is_file() and f.suffix in (".jar", ".class", ".war", ".ear"):
            try:
                f.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Standalone readers, for artifacts you produced yourself
# --------------------------------------------------------------------------- #
def jar_of(path) -> jarinspect.Jar:
    """Open any jar by path, with the same reader `Tree.jar` returns."""
    return jarinspect.Jar(Path(path))


def zip_entries(path) -> list[str]:
    with zipfile.ZipFile(str(path)) as z:
        return sorted(i.filename for i in z.infolist() if not i.is_dir())


def zip_read(path, member: str) -> bytes:
    with zipfile.ZipFile(str(path)) as z:
        return z.read(member)


def class_bytes_major(data: bytes) -> int:
    """The class-file major version: 52 is release 8, 53 is 9, 61 is 17."""
    return classfile.major_version(data)


def class_strings(data: bytes) -> set[str]:
    """Every string constant in a class's constant pool.

    Where a compiled-in value becomes visible from outside: a version string a
    template was supposed to filter, a constant a build was supposed to recompile
    rather than copy.
    """
    return set(classfile.ClassFile(data).strings())


def class_module(data: bytes) -> dict:
    """A `module-info.class` decoded: name, requires, exports, version.

    `.parse()` before `.module()`: the constant pool is read in the constructor but
    the attribute table is not, and the Module attribute lives in the latter.
    """
    return classfile.ClassFile(data).parse().module()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
