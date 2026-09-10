#!/usr/bin/env python3
"""Which build system the delivered tree declares, and how to drive it.

This stage grades artefacts, not build scripts.  Every check in the nine modules
downstream reads a jar, a class file, a manifest, a published repository or a
JUnit report -- none of them reads a `build.gradle` or a `pom.xml`.  That is the
line stage 2 is built around, and it has a consequence worth drawing out: if no
check reads the build scripts, then nothing in this stage needs to *assume* which
build system produced the artefacts.  Only the driver does.

Why that matters
----------------
State A is the oracle.  Every expected entry, every expected class byte, every
expected manifest header in `data/` was recorded from a State A build, and the
suite's claim is "your artefacts match State A's".  A stage that cannot build
State A cannot demonstrate its own expectations are satisfiable -- it asserts a
target without ever showing the target is reachable, and a single wrong byte in
the frozen ground truth would be indistinguishable from a submission's mistake.
Worse, it would charge a *correct* migration for a defect in the oracle.

So the driver speaks both dialects.  It detects what the delivered tree declares
and translates each configuration in the matrix into that dialect's own
invocation.  A Gradle submission is driven as a Gradle build; State A is driven
with Maven and reaches the same eight trees.  Nothing else in the suite is
affected, because nothing else in the suite knows.

This does not make the task satisfiable by changing nothing.  Stage 1 asks
whether the migration happened -- `maven_retired` and `gradle_is_the_build` are
required gates, read by a reviewer over both trees -- and a submission that fails
either scores zero before this image runs.  The division of labour is the point:
stage 1 judges the repository, stage 2 measures the build's output, and neither
does the other's job badly.

The vocabulary
--------------
A configuration names an *intent*, not a task list:

    assemble          the four jars, without running tests
    build             the four jars, and the test task
    publish           the four jars, and a publication into a local repository
    assemble+publish  both, as two explicit steps in one invocation

Each dialect renders those into its own argv, and reports where it puts its
output.  Everything a check needs -- `<project>/build` vs `<project>/target`,
`build/test-results` vs `target/surefire-reports` -- comes from here.
"""
from __future__ import annotations

import os
import re

MAVEN = "maven"
GRADLE = "gradle"

INTENTS = ("assemble", "build", "publish", "assemble+publish")

#: The version the `version` configuration supplies is passed through `props`
#: under this key, in both dialects.  What each dialect *does* with it differs:
#: Gradle takes a project property on the command line, Maven has no such thing
#: and the version lives in the POM, so it is set there.  Both are "one input".
VERSION_PROP = "version"

#: The property naming the publication directory, from the build contract.
PUBLISH_PROP = "srbPublishDir"


# ----------------------------------------------------------------- detection --
def detect(root):
    """Which build system the delivered tree declares, or None.

    Gradle wins when both are present.  A tree that still carries `pom.xml`
    beside a working Gradle build has not finished the migration -- which is
    stage 1's judgement to make, over the files, with a reviewer -- but the thing
    to *drive* here is the build the submission says is its build, and a leftover
    POM must not make this stage run the toolchain the submission retired.
    """
    gradle = any(os.path.isfile(os.path.join(root, n)) for n in
                 ("settings.gradle", "settings.gradle.kts",
                  "build.gradle", "build.gradle.kts"))
    if gradle:
        return GRADLE
    if os.path.isfile(os.path.join(root, "pom.xml")):
        return MAVEN
    return None


# ------------------------------------------------------------------- Gradle ---
class Gradle:
    """The dialect the migration is *to*.  Driven exactly as before."""

    name = GRADLE
    program = os.environ.get("SRB_GRADLE", "gradle")

    #: Engines this dialect legitimately needs on PATH.  Everything else in
    #: make_stubs is shadowed by a failing recorder.  `gradlew` is not here: a
    #: wrapper would fetch a distribution over a network that does not exist, and
    #: a build that needs it has made this suite's `gradle` not the build.
    keep_on_path = ("gradle",)

    #: Where a project's build output lands, relative to the project directory.
    build_dirs = ("build",)

    #: Where the test task writes JUnit XML, relative to the project directory.
    #: Searched, not assumed -- these only narrow the walk.
    test_dirs = ("build/test-results", "build")

    #: Directories `prepare`/`scrub` treat as output rather than source.
    output_dirs = (".gradle", "build")

    @staticmethod
    def argv(intent, props, publish_dir, root, extra_args=()):
        base = [Gradle.program, "--offline", "--no-daemon", "--console=plain",
                "--stacktrace", "--warning-mode=none"]
        for k, v in sorted((props or {}).items()):
            base.append("-P%s=%s" % (k, v))
        base.extend(extra_args)
        return base + {"assemble": ["clean", "assemble"],
                       "build": ["clean", "build"],
                       "publish": ["clean", "publish"],
                       "assemble+publish":
                           ["clean", "assemble", "publish"]}[intent]

    @staticmethod
    def prepare(root, props):
        """Nothing: Gradle takes the version as a command-line property."""
        return []


# -------------------------------------------------------------------- Maven ---
#: The settings the environment installs, with `<offline>true</offline>` in it.
_SETTINGS = os.environ.get("SRB_MAVEN_SETTINGS", "/root/.m2/settings.xml")
#: A copy with that element removed.  maven-deploy-plugin refuses to run at all
#: when the session is offline -- even to a `file:` repository, where there is
#: nothing to reach -- so the publishing configurations use this and drop `-o`.
#: Nothing becomes resolvable: the container has no network either way.
_DEPLOY_SETTINGS = "/tmp/srb-maven-settings-deploy.xml"


def _deploy_settings():
    if not os.path.isfile(_DEPLOY_SETTINGS):
        try:
            with open(_SETTINGS, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            return _SETTINGS
        text = re.sub(r"[ \t]*<offline>[^<]*</offline>\s*\n?", "", text)
        with open(_DEPLOY_SETTINGS, "w", encoding="utf-8") as fh:
            fh.write(text)
    return _DEPLOY_SETTINGS


class Maven:
    """State A's dialect.  Present so this stage can build its own oracle."""

    name = MAVEN
    program = os.environ.get("SRB_MAVEN", "mvn")
    keep_on_path = ("mvn",)
    build_dirs = ("target",)
    test_dirs = ("target/surefire-reports", "target/failsafe-reports", "target")
    output_dirs = ("target",)

    @staticmethod
    def argv(intent, props, publish_dir, root, extra_args=()):
        """Maven's rendering of one intent.

        `publish` is `deploy` to a `file:` repository under the project, which is
        the same thing `-P<publish_property>` names for Gradle: the property's
        value is resolved against the project and a repository appears there.
        `-Prelease` is State A's own profile for what a release publishes -- it is
        where `maven-source-plugin` attaches the sources jar the contract requires
        -- with signing and javadoc turned off, since neither is in the artefact
        set this suite reads and a signing key does not exist here.
        """
        props = dict(props or {})
        props.pop(VERSION_PROP, None)       # applied to the POM by prepare()
        props.pop(PUBLISH_PROP, None)       # becomes altDeploymentRepository
        publishing = intent in ("publish", "assemble+publish")

        cmd = [Maven.program, "-B", "--no-transfer-progress"]
        if publishing:
            cmd += ["--settings", _deploy_settings()]
        else:
            cmd += ["-o", "--settings", _SETTINGS]
        for k, v in sorted(props.items()):
            cmd.append("-D%s=%s" % (k, v))
        cmd.extend(extra_args)

        # Only `build` runs the test task -- Gradle's `assemble` and `publish` do
        # not depend on `check`, so neither may their Maven renderings.
        if intent != "build":
            cmd.append("-DskipTests")

        if not publishing:
            return cmd + ["clean", "package"]

        where = publish_dir or "srb-publish"
        if not os.path.isabs(where):
            where = os.path.join(root, where)
        return cmd + [
            "-Prelease", "-Dgpg.skip=true", "-Dmaven.javadoc.skip=true",
            # The local repository is the vendored offline closure, and the image
            # asserts no gson 2.10.1 is in it.  Installing would put one there and
            # let a later configuration resolve a prebuilt jar instead of
            # building one.
            "-Dmaven.install.skip=true",
            "-DaltDeploymentRepository=srb::default::file://%s" % where,
            "clean", "deploy"]

    @staticmethod
    def prepare(root, props):
        """Apply the version input, which in Maven means setting it in the POM.

        Gradle's `-Pversion=X` has no Maven equivalent, and the reason is not an
        omission in Maven's command line: in Maven the version *is* the POM's
        `<version>`, and supplying a different one means writing it there.  That
        is what `mvn versions:set` and `mvn release:prepare` both do, and what
        this reproduces -- the reactor root's own `<version>`, plus the
        `<parent><version>` back-reference each module carries, which Maven
        requires to be a literal and which therefore moves with it.

        Still one input.  The old version is read from the root POM rather than
        assumed, and every occurrence of *that* value in a `<version>` element is
        replaced, so five files change and no decision is made five times.  A
        `<version>` on a dependency that happened to share the value would move
        too, and is not a case this reactor has: gson's own modules resolve each
        other through the reactor, not by a pinned version.

        Returns a list of (path, changed) for the ledger, so a version
        configuration whose input never landed is visible as such rather than as
        a build that mysteriously produced the old version.
        """
        want = (props or {}).get(VERSION_PROP)
        if not want:
            return []
        root_pom = os.path.join(root, "pom.xml")
        try:
            with open(root_pom, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            return []
        # The reactor root's own <version>: the first one that is not inside a
        # <parent> block.  State A's root POM has no <parent> at all.
        head = re.split(r"<(?:parent|dependencies|build|profiles)\b", text, 1)[0]
        m = re.search(r"<version>\s*([^<\s]+)\s*</version>", head)
        if not m:
            return []
        old = m.group(1)
        if old == want:
            return []
        pattern = re.compile(r"(<version>\s*)%s(\s*</version>)"
                             % re.escape(old))
        out = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in ("target", ".git", "build", ".gradle")]
            if "pom.xml" not in filenames:
                continue
            path = os.path.join(dirpath, "pom.xml")
            try:
                with open(path, encoding="utf-8") as fh:
                    src = fh.read()
                new, n = pattern.subn(r"\g<1>%s\g<2>" % want, src)
                if n:
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(new)
                out.append((os.path.relpath(path, root), n))
            except OSError as exc:
                out.append((os.path.relpath(path, root), "error: %r" % (exc,)))
        return out


DIALECTS = {GRADLE: Gradle, MAVEN: Maven}


def get(name):
    """The dialect object for a name, defaulting to Gradle.

    Gradle is the default rather than an error: it is the build system this task
    migrates *to*, so a tree whose declaration could not be read is driven the way
    a submission is expected to be driven, and the failure that follows is
    reported against the build rather than against the harness.
    """
    return DIALECTS.get(name, Gradle)


def for_tree(root):
    return get(detect(root))
