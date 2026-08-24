#!/usr/bin/env python3
"""What a consumer resolves.

A build tool's real output is not the jar in ``build/libs`` -- it is the artifact
set someone else downloads: the jar, the sources jar, and a POM whose
coordinates, licence, SCM and dependency list are correct.  Maven derives that
POM from the project model for free.  Gradle produces one only if a publication
is configured, and its defaults get it wrong in ways that stay invisible until a
consumer resolves the result: no licence block, no developer, and -- most
consequentially -- ``runtime`` dependencies invented from the ``implementation``
and ``api`` configurations.

That last one is the trap this module exists for.  gson 2.10.1 has no runtime
dependencies at all; it compiles against ``error_prone_annotations`` and tests
against JUnit, and State A scopes both so that neither reaches the published POM.
Under Gradle the obvious translation of a compile dependency publishes it, so
every consumer of the migrated artifact silently acquires a transitive dependency
the library never had.  No compile step and no test run detects that.  Reading
what was published is the only place it becomes visible.

State A publishes exactly one module.  The other three exist to be built and
tested, and publishing them would be a regression in the opposite direction: three
artifacts released under coordinates that have never been released.

This module reads the `publish` configuration's repository.  Nothing here looks at
a build script: whether a submission applied ``maven-publish`` to every project or
to one is a question about code, asked in stage 1 by reading it, and the answer
that matters here is which artifacts came out.

What "the POM says" means
-------------------------
The model a consumer resolves, not the bytes of one file.  A POM may declare a
``<parent>`` and inherit half its metadata from it -- Maven's own release of gson
2.10.1 does exactly that, and publishes ``gson-parent`` beside it because a
resolver has to fetch the parent before it can compute anything -- or it may be
flattened, which is what Gradle's ``maven-publish`` writes.  Both are correct
publications and they resolve to the same model, so the metadata checks read
through the chain (``Pom.inherited``) and the structural ones ask the question that
actually matters about a parent: not whether there is one, but whether a consumer
could resolve it.

Two checks here are recorded at weight 0.0.  Both are true of a Gradle publication
and false of a Maven one while describing nothing a consumer receives -- a
``<build>`` section nobody downstream executes, and a test-scoped dependency that
no resolver ever puts on a classpath.  They stay in the report because each is a
clear signal about who produced the file, and they stay out of the arithmetic
because "who produced it" is stage 1's question, gated there.
"""
import json
import os
import re

import pytest

import jarinspect

DATA = os.path.join(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"), "data")
with open(os.path.join(DATA, "pom.json")) as _fh:
    POM = json.load(_fh)
with open(os.path.join(DATA, "build_contract.json")) as _fh:
    CONTRACT = json.load(_fh)
with open(os.path.join(DATA, "sources.json")) as _fh:
    SOURCES = json.load(_fh)

GROUP = POM["groupId"]
ARTIFACT = POM["artifactId"]
VERSION = POM["version"]

#: Every main source root of :gson, as State A lays them out.  The sources jar
#: has to carry all of it -- including the one file that does not exist until the
#: build generates it.
_MAIN_JAVA = "gson/src/main/java/"
_MAIN_TEMPLATES = "gson/src/main/java-templates/"
JAVA_SOURCE_COUNT = len([p for p in SOURCES["state_a_inventory"]
                         if p.endswith(".java")
                         and (p.startswith(_MAIN_JAVA)
                              or p.startswith(_MAIN_TEMPLATES))])

BUILD_CONFIG_SOURCE = "com/google/gson/internal/GsonBuildConfig.java"
DESCRIPTOR = "META-INF/versions/9/module-info.class"

#: Maven plugin coordinates a published POM has no business carrying.  A POM that
#: names them is State A's ``pom.xml`` under a new file name.
MAVEN_PLUGIN_MARKERS = ("bnd-maven-plugin", "templating-maven-plugin",
                        "moditect-maven-plugin", "proguard-maven-plugin",
                        "maven-surefire-plugin", "maven-compiler-plugin")


def _listing(root, limit=40):
    """Every file under `root`, relative, for a failure message."""
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in sorted(filenames):
            out.append(os.path.relpath(os.path.join(dirpath, fn), root))
    out.sort()
    if len(out) > limit:
        return "\n".join(out[:limit] + ["... and %d more" % (len(out) - limit)])
    return "\n".join(out) or "(empty)"


def _rel(*parts):
    return os.path.join(*(GROUP.split(".") + list(parts)))


def _has_source_for(entry, sources):
    """Is some ``.java`` in `sources` the file this class was compiled from?

    A nested class lives in its outer class's file, so the source of
    ``a/B$C.class`` is ``a/B.java``.  Splitting at the first ``$`` gets that
    wrong here: gson has five classes whose *own* names begin with one --
    ``$Gson$Types``, ``$Gson$Preconditions`` -- and their file names begin with
    ``$`` too.  So the rule is the one a decompiler applies: the source is the
    longest prefix of the binary name that is itself a file, and the whole name
    is tried before any prefix.
    """
    stem = entry[:-len(".class")]
    while True:
        if stem + ".java" in sources:
            return True
        cut = stem.rfind("$")
        if cut <= 0 or stem[cut - 1] == "/":
            return False
        stem = stem[:cut]


# --------------------------------------------------------------- fixtures --
@pytest.fixture(scope="session")
def publish_root(b_publish):
    """The Maven repository the `publish` configuration wrote."""
    root = b_publish.publish_root()
    if root is None:
        pytest.fail(
            "`gradle -P%s=%s publish` produced no repository.\n"
            "The build contract names that property and says the directory is "
            "resolved against the project, so a maven publication has to write "
            "there; without one there is no published artifact to inspect.\n\n%s"
            % (CONTRACT["publish_property"], "srb-publish",
               "" if b_publish.ok else b_publish.failure_summary()))
    return root


@pytest.fixture(scope="session")
def published_pom(publish_root):
    """gson's published POM, with the repository it was published into.

    The second argument is what lets the checks below read the model a consumer
    resolves rather than the one file: ``Pom.inherited()`` walks the ``<parent>``
    chain through `publish_root`, exactly as a resolver walks it through a remote
    repository.  A flattened POM has a chain of one and reads identically.
    """
    rel = _rel(ARTIFACT, VERSION, "gson-%s.pom" % VERSION)
    p = os.path.join(publish_root, rel)
    if not os.path.isfile(p):
        pytest.fail("no %s in the published repository.\nPublished instead:\n%s"
                    % (rel, _listing(publish_root)))
    return jarinspect.Pom(p, repo_root=publish_root)


@pytest.fixture(scope="session")
def published_sources(publish_root):
    rel = _rel(ARTIFACT, VERSION, "gson-%s-sources.jar" % VERSION)
    p = os.path.join(publish_root, rel)
    if not os.path.isfile(p):
        pytest.fail("no sources jar published at %s.\nPublished instead:\n%s"
                    % (rel, _listing(publish_root)))
    jar = jarinspect.Jar(p)
    yield jar
    jar.close()


@pytest.fixture(scope="session")
def published_jar(publish_root):
    rel = _rel(ARTIFACT, VERSION, "gson-%s.jar" % VERSION)
    p = os.path.join(publish_root, rel)
    if not os.path.isfile(p):
        pytest.fail("no jar published at %s.\nPublished instead:\n%s"
                    % (rel, _listing(publish_root)))
    jar = jarinspect.Jar(p)
    yield jar
    jar.close()


# ----------------------------------------------------- the publication runs --
@pytest.mark.audit
def test_publish_task_succeeds(b_publish):
    """`gradle publish` completes.  Without it there is nothing to consume."""
    assert b_publish.ok, b_publish.failure_summary()


@pytest.mark.audit
def test_publication_produces_a_repository(publish_root):
    assert os.path.isdir(publish_root), \
        "%s is not a directory" % publish_root


@pytest.mark.audit
def test_published_pom_is_readable_xml(published_pom):
    """The POM parses and names an artifact.

    Read first, because every check below reports a detail of a document this one
    says exists at all.
    """
    assert published_pom.text("artifactId") is not None, \
        "the published POM has no <artifactId>"


@pytest.mark.behavioural
def test_published_pom_declares_the_maven_model_version(published_pom):
    """``<modelVersion>4.0.0</modelVersion>``.

    Cheap, and it is what makes the file a POM rather than XML that happens to
    have the right element names.
    """
    got = published_pom.text("modelVersion")
    assert got == "4.0.0", "published <modelVersion> is %r, want '4.0.0'" % got


# ------------------------------------------------------------ the artifacts --
@pytest.mark.behavioural
@pytest.mark.parametrize("filename", POM["required_sibling_files"])
def test_published_artifact_present(publish_root, filename):
    """The jar, the sources jar and the POM are all published.

    A publication that omits ``-sources`` still resolves; it just leaves every
    consumer's debugger without source.  State A attaches it, so this does too.
    """
    rel = _rel(ARTIFACT, VERSION, filename)
    p = os.path.join(publish_root, rel)
    assert os.path.isfile(p), (
        "%s was not published.\nPublished files:\n%s"
        % (rel, _listing(publish_root)))
    assert os.path.getsize(p) > 0, "%s is empty" % rel


@pytest.mark.behavioural
def test_published_repository_layout(publish_root):
    """Coordinates map to the standard group/artifact/version path.

    A publication that writes a flat directory of files resolves for nobody: the
    layout *is* the protocol.
    """
    rel = _rel(ARTIFACT, VERSION)
    d = os.path.join(publish_root, rel)
    assert os.path.isdir(d), (
        "expected the Maven layout %s under the publication root; found:\n%s"
        % (rel, _listing(publish_root)))


@pytest.mark.behavioural
def test_only_one_version_is_published(publish_root):
    """One version directory under the artifact, and it is 2.10.1.

    A build that publishes both its own version and a hardcoded one leaves a
    consumer resolving whichever the metadata points at.
    """
    d = os.path.join(publish_root, _rel(ARTIFACT))
    if not os.path.isdir(d):
        pytest.fail("nothing published under %s" % _rel(ARTIFACT))
    got = sorted(x for x in os.listdir(d)
                 if os.path.isdir(os.path.join(d, x)))
    assert got == [VERSION], \
        "version directories under %s are %s, want %s" % (
            _rel(ARTIFACT), got, [VERSION])


@pytest.mark.behavioural
def test_no_foreign_artifact_in_the_version_directory(publish_root):
    """Every published file belongs to the artifact whose directory it is in.

    Gradle writes checksums and, if module metadata is enabled, a ``.module``
    file beside the three required ones, so the file *set* is not fixed.  What is
    fixed is the prefix: a ``gson-extras-2.10.1.jar`` sitting in gson's directory
    is a publication that pointed two projects at one set of coordinates.
    """
    d = os.path.join(publish_root, _rel(ARTIFACT, VERSION))
    if not os.path.isdir(d):
        pytest.skip("no %s directory to inspect" % _rel(ARTIFACT, VERSION))
    prefix = "%s-%s" % (ARTIFACT, VERSION)
    bad = sorted(fn for fn in os.listdir(d)
                 if os.path.isfile(os.path.join(d, fn))
                 and not fn.startswith(prefix)
                 and not fn.startswith("maven-metadata"))
    assert not bad, (
        "files published under %s that do not belong to %s: %s"
        % (_rel(ARTIFACT, VERSION), prefix, bad))


# -------------------------------------------------------- POM coordinates --
@pytest.mark.audit
def test_pom_group_id(published_pom):
    """``com.google.code.gson``, as a consumer resolves it.

    Read through the parent chain: ``<groupId>`` is one of the fields Maven
    inherits, so a POM that omits it beside a published parent that declares it
    resolves to the right coordinates.  A POM that omits it with no resolvable
    parent has no group at all, and reads as None here -- which fails, as it
    should.
    """
    got = published_pom.coordinates()[0]
    assert got == GROUP, "published groupId is %r, want %r" % (got, GROUP)


@pytest.mark.audit
def test_pom_artifact_id(published_pom):
    got = published_pom.text("artifactId")
    assert got == ARTIFACT, \
        "published artifactId is %r, want %r" % (got, ARTIFACT)


@pytest.mark.audit
def test_pom_version(published_pom):
    """``2.10.1``, as a consumer resolves it -- and the directory it sits in.

    ``<version>`` inherits like ``<groupId>``, so it is read the same way.  The
    second assertion is what keeps that from being a weaker claim: a POM whose
    resolved version disagrees with the directory it was published into is
    unresolvable at the coordinates it occupies, whichever of the two is wrong.
    """
    got = published_pom.coordinates()[2]
    assert got == VERSION, "published version is %r, want %r" % (got, VERSION)
    holder = os.path.basename(os.path.dirname(published_pom.path))
    assert holder == VERSION, (
        "the POM resolves to version %r but was published under %r; no consumer "
        "asking for either one would get a POM that agrees with itself"
        % (got, holder))


@pytest.mark.behavioural
def test_pom_packaging(published_pom):
    """``jar``.  Absent means jar, so either spelling is accepted."""
    got = published_pom.text("packaging")
    assert got in (None, POM["packaging"]), \
        "published packaging is %r, want %r" % (got, POM["packaging"])


@pytest.mark.behavioural
def test_pom_coordinates_resolve_to_the_published_path(published_pom,
                                                       publish_root):
    """The resolved triple is the one the repository layout promises.

    The three checks above read the fields; this one states the property they are
    for.  A repository is a map from coordinates to files, and the map has to agree
    with itself: whatever a POM at ``com/google/code/gson/gson/2.10.1/`` resolves
    to must be ``com.google.code.gson:gson:2.10.1``, or a consumer that asked for
    those coordinates has been handed the description of something else.

    Path first, then model, so a publication that got the layout right and the
    document wrong is reported as the mismatch it is rather than as three separate
    missing fields.
    """
    got = published_pom.coordinates()
    rel = os.path.relpath(os.path.dirname(published_pom.path), publish_root)
    parts = rel.replace(os.sep, "/").split("/")
    want = (".".join(parts[:-2]), parts[-2], parts[-1])
    assert got == want, (
        "the POM published at %s resolves to %r; a consumer resolving %r would "
        "receive it and find it describing something else." % (rel, got, want))
    assert got == (GROUP, ARTIFACT, VERSION), \
        "resolved coordinates are %r, want %r" % (
            got, (GROUP, ARTIFACT, VERSION))


# ---------------------------------------------------------- POM metadata --
@pytest.mark.behavioural
def test_pom_name(published_pom):
    """``Gson``, read from this file and not through the chain.

    ``<name>`` is one of the handful of elements Maven does not inherit -- with
    ``artifactId``, ``packaging`` and ``<modules>`` -- because a parent's name
    describes the parent.  So the plain read is the correct one here, and a POM
    that left its name to be inherited would have no name at all.
    """
    got = published_pom.text("name")
    assert got == POM["name"], \
        "published <name> is %r, want %r" % (got, POM["name"])


@pytest.mark.behavioural
def test_pom_description(published_pom):
    got = published_pom.inherited("description")
    assert got == POM["description"], \
        "published <description> resolves to %r, want %r" % (
            got, POM["description"])


@pytest.mark.behavioural
def test_pom_url(published_pom):
    """The project URL a consumer resolves.

    Compared against the declared value, which is why this reads the chain rather
    than asking Maven: Maven's effective model appends the module's artifactId to
    an inherited ``url``, so gson's effective url is
    ``https://github.com/google/gson/gson``.  That suffix is bookkeeping about
    where a module sat in a reactor and says nothing a consumer wants; the
    declared value is the project's URL in both dialects.
    """
    got = published_pom.inherited("url")
    assert got == POM["url"], \
        "published <url> resolves to %r, want %r" % (got, POM["url"])


@pytest.mark.behavioural
def test_pom_declares_one_license(published_pom):
    """Exactly as many ``<license>`` blocks as State A declares.

    Gradle's default publication carries no licence at all -- it has to be
    written into the ``pom { }`` block -- and a POM without one is rejected by
    Maven Central's own validation before a consumer ever sees it.
    """
    got = published_pom.inherited_findall("license")
    assert len(got) == len(POM["licenses"]), (
        "the published POM resolves to %d license(s), State A declares %d"
        % (len(got), len(POM["licenses"])))


@pytest.mark.behavioural
@pytest.mark.parametrize("lic", POM["licenses"],
                         ids=[l["name"] for l in POM["licenses"]])
def test_pom_license(published_pom, lic):
    """This licence's name and URL are both present."""
    found = published_pom.inherited_findall("license")
    names = [published_pom.child_text(e, "name") for e in found]
    urls = [published_pom.child_text(e, "url") for e in found]
    assert lic["name"] in names, \
        "license %r missing from the published POM (found %s)" % (
            lic["name"], names)
    assert lic["url"] in urls, \
        "license URL %r missing from the published POM (found %s)" % (
            lic["url"], urls)


@pytest.mark.behavioural
def test_pom_declares_developer(published_pom):
    devs = published_pom.inherited_findall("developer")
    assert len(devs) == len(POM["developers"]), \
        "the published POM resolves to %d developer(s), State A declares %d" % (
            len(devs), len(POM["developers"]))


@pytest.mark.behavioural
def test_pom_developer_organization(published_pom):
    orgs = [published_pom.child_text(e, "organization")
            for e in published_pom.inherited_findall("developer")]
    want = [d["organization"] for d in POM["developers"] if d["organization"]]
    for org in want:
        assert org in orgs, \
            "developer organization %r missing (found %s)" % (org, orgs)


@pytest.mark.behavioural
@pytest.mark.parametrize("field", sorted(POM["scm"]))
def test_pom_scm_field(published_pom, field):
    """SCM coordinates survive the migration.

    These are what Maven Central requires and what tooling uses to find the
    source of a released artifact.  Read through the chain, and against the
    declared value for the same reason as ``url``: Maven's effective model
    appends the artifactId to inherited ``scm`` entries too.
    """
    want = POM["scm"][field]
    got = published_pom.inherited("scm/" + field)
    assert got == want, \
        "published <scm><%s> resolves to %r, want %r" % (field, got, want)


# --------------------------------------------------------- the dependencies --
@pytest.mark.audit
def test_pom_has_no_runtime_dependencies(published_pom):
    """gson 2.10.1 has no dependency a consumer resolves.  The POM must say so.

    The migration's most consequential trap, and the reason this module carries an
    audit marker at all.  gson compiles against ``error_prone_annotations``
    and tests against JUnit and Truth; State A scopes them so none reaches a
    consumer.  In Gradle, ``implementation`` and ``api`` both publish as runtime
    dependencies, so the natural translation gives every gson consumer a
    transitive dependency it never had -- a shipped regression that no compile and
    no test would catch.

    Every scope except ``test`` counts, which is one more than the name suggests
    and deliberate: ``provided`` and ``system`` are not transitive either, but
    State A declares neither, and a POM that carries one is describing a model the
    library does not have.  ``test`` is excluded because State A's own release
    publishes JUnit at that scope -- Maven writes the module's declarations out and
    leaves the scoping to the consumer's resolver -- so demanding its absence here
    would demand that a Maven publication be a Gradle one.  That difference is
    reported by the next check, at weight zero.
    """
    deps = published_pom.dependencies()
    leaked = [d for d in deps
              if (d.get("scope") or "compile") != "test"]
    assert not leaked, (
        "the published POM adds %d dependenc(ies) that gson 2.10.1 does not "
        "declare: %s\n"
        "compileOnly (or an equivalent) is what keeps a compile-time-only "
        "annotation dependency out of a published POM; implementation and api "
        "both publish."
        % (len(leaked), ", ".join("%s:%s (%s)" % (d["groupId"], d["artifactId"],
                                                  d["scope"]) for d in leaked)))


@pytest.mark.srb_weight(0.0)
@pytest.mark.behavioural
def test_pom_declares_no_test_dependencies(published_pom):
    """No test-scoped dependency in the published POM.  Reported, not scored.

    A test scope reaches no consumer.  Maven's resolver drops it at the first
    transitive hop by definition, Gradle's Module Metadata has no variant that
    could carry it, and no dependency report a consumer runs will list it -- so
    what this check finds is not a difference in what anybody downloads.

    It is still worth reporting, because it is a clean signal about the producer.
    State A's own release publishes gson's POM with JUnit at ``<scope>test</scope>``
    -- Maven writes the module's declared dependencies out and lets the consumer's
    resolver do the scoping -- while Gradle's ``maven-publish`` maps
    ``testImplementation`` to nothing at all and writes a POM with no dependency
    section.  Both are correct publications of the same library.  Which one this
    file is tells you which tool wrote it, and that is stage 1's question, gated
    there; charging for it here would charge twice for one answer.
    """
    deps = published_pom.dependencies()
    tests = [d for d in deps if (d.get("scope") or "") == "test"]
    assert not tests, (
        "the published POM carries %d test-scoped dependenc(ies): %s"
        % (len(tests), ", ".join(d["artifactId"] for d in tests)))


@pytest.mark.srb_weight(0.0)
@pytest.mark.behavioural
def test_pom_declares_no_dependencies_at_all(published_pom):
    """Not in any scope.  Reported, not scored, for the same reason.

    Every scope but ``test`` is already asserted at full weight by
    ``test_pom_has_no_runtime_dependencies``, so the only thing this adds to the
    arithmetic is the test scope -- which is the check above's subject, and scores
    nothing there.  It would be strange for the union of two checks to score what
    neither part does, so this states the tidier requirement and scores nothing.
    """
    deps = published_pom.dependencies()
    assert not deps, (
        "the published POM declares %d dependenc(ies): %s\n"
        "State A's published gson POM declares none a consumer resolves; JUnit at "
        "test scope is Maven's spelling of a dependency nobody downstream sees."
        % (len(deps), ", ".join("%s:%s (%s)" % (d["groupId"], d["artifactId"],
                                                d["scope"]) for d in deps)))


@pytest.mark.behavioural
def test_pom_does_not_depend_on_itself_or_siblings(published_pom):
    """No sibling module leaks into gson's POM.

    gson is a leaf: extras, metrics and proto depend on it, never the reverse.  A
    publication configured on the wrong project publishes the wrong graph, and
    this is the shape that mistake takes.
    """
    # artifactIds, not jar names: the proto module's artifactId is `proto` and only
    # its <finalName> is `gson-proto`, so a set naming just the latter would miss a
    # POM that declared a dependency on the sibling it is most likely to name.
    bad = {"gson", "gson-extras", "gson-metrics", "proto", "gson-proto",
           "gson-parent"}
    hits = [d["artifactId"] for d in published_pom.dependencies()
            if d["artifactId"] in bad]
    assert not hits, "gson's published POM depends on %s" % hits


# ------------------------------------------------- not State A's POM again --
@pytest.mark.audit
def test_pom_parent_is_resolvable(published_pom):
    """A ``<parent>``, if there is one, is published where a consumer can fetch it.

    The requirement is resolvability, not flatness.  A resolver fetches the parent
    before it can compute anything, so a POM naming a parent that was never
    published is unresolvable -- the consumer gets a build failure rather than a
    library.  That is the defect, and it is worth an audit marker.

    Being flat is not the defect.  Maven's own release of gson 2.10.1 declares
    ``com.google.code.gson:gson-parent:2.10.1`` and publishes it beside gson for
    exactly this reason; Gradle's ``maven-publish`` writes the flattened form and
    publishes nothing extra.  Both resolve.  Demanding the second would demand
    that the reference release of this library be a defect, which is the opposite
    of what this stage measures.

    ``Pom.parent()`` resolves through the repository layout rather than through
    ``<relativePath>``, because a consumer has one file and a repository, not a
    checkout.
    """
    coords = published_pom.parent_coordinates()
    if coords is None:
        assert not published_pom.findall("parent"), (
            "the published POM declares a <parent> without a complete "
            "groupId/artifactId/version, so no consumer could resolve it")
        return
    assert published_pom.parent() is not None, (
        "the published POM declares <parent> %s:%s:%s, and no POM for it was "
        "published in this repository (expected at %s).  A resolver fetches the "
        "parent before it can compute anything, so a consumer could not resolve "
        "gson at all."
        % (coords + (os.path.relpath(published_pom.parent_path(),
                                     published_pom.repo_root),)))


@pytest.mark.behavioural
def test_pom_declares_no_modules(published_pom):
    """A published artifact's POM is not a reactor description.

    Read as ``project/modules/module`` rather than by searching the document for
    that element name, because the name is not reserved: moditect's plugin
    configuration contains a ``<module>`` block naming the module-info source, and
    State A's POM carries one.  A search would report the aggregator this check
    exists to catch and also every POM that configures moditect -- and it would
    report them identically, which is the worse half.
    """
    node = published_pom.root.find(jarinspect.POM_NS + "modules")
    if node is None:
        node = published_pom.root.find("modules")
    mods = [] if node is None else (
        node.findall(jarinspect.POM_NS + "module") or node.findall("module"))
    assert not mods, (
        "the published POM's <modules> lists %d module(s): %s\n"
        "It is describing a reactor rather than an artifact."
        % (len(mods), [m.text for m in mods]))


@pytest.mark.srb_weight(0.0)
@pytest.mark.behavioural
def test_pom_declares_no_maven_build(published_pom):
    """No ``<build>`` section naming Maven plugins.

    Gradle's ``maven-publish`` never writes one, so a POM that carries bnd,
    templating, moditect or surefire is State A's ``pom.xml`` copied into the
    repository layout rather than a publication the migrated build produced.

    Recorded at weight 0.0.  A ``<build>`` section is inert for a consumer -- nobody
    resolving this artifact runs the producer's plugins -- so this check says nothing
    about the artifact and everything about who produced it, which makes it the one
    check in this module that is migration detection rather than publication
    behaviour.  State A's published POM carries these markers by construction.  The
    judgement is stage 1's ``maven_retired``, and a gate failure there scores the
    submission zero before this image runs.  It is still worth recording: a POM that
    names surefire is the clearest single signal that the publication was copied
    rather than generated.
    """
    if published_pom.path is None:
        pytest.skip("the POM was read from memory")
    with open(published_pom.path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    hits = sorted(m for m in MAVEN_PLUGIN_MARKERS if m in text)
    assert not hits, (
        "the published POM names Maven plugins: %s\n"
        "A publication generated by the migrated build describes the artifact, "
        "not how Maven used to produce it." % hits)


#: Sections of a POM a consumer never reads.  `build` and `reporting` configure the
#: producer's own plugins, `properties` supplies values to them, and `profiles` is
#: all three again under a condition.  A resolver reads none of it: it wants
#: coordinates, packaging, dependencies and metadata.
_PRODUCER_SECTIONS = ("build", "reporting", "properties", "profiles")


def _consumer_facing_text(pom):
    """Every text and attribute value in `pom` that a consumer resolves.

    A flat walk of the document with the producer's own sections pruned.  Returns a
    list of ``(path, value)`` so a failure can say where it found the thing rather
    than only what it was.
    """
    out = []

    def walk(node, path):
        for child in node:
            tag = child.tag.split("}")[-1]
            if tag in _PRODUCER_SECTIONS:
                continue
            here = "%s/%s" % (path, tag)
            for value in [child.text] + list(child.attrib.values()):
                if value:
                    out.append((here, value))
            walk(child, here)

    walk(pom.root, "project")
    return out


@pytest.mark.behavioural
def test_pom_has_no_unresolved_placeholders(published_pom):
    """No ``${...}`` in a field a consumer resolves.

    An unexpanded property is what a hand-written POM template looks like when
    nothing substituted into it, and a consumer's resolver treats it as a literal:
    ``<version>${project.version}</version>`` publishes an artifact at the
    coordinates ``com.google.code.gson:gson:${project.version}``, which nobody can
    depend on.

    Scoped to the consumer-facing model, because inside ``<build>`` a ``${...}`` is
    not a defect but the point.  State A's POM says
    ``<sourceDirectory>${basedir}/src/...</sourceDirectory>`` and Maven expands it
    when it runs the producer's plugins, in the producer's checkout; a literal path
    there would be the bug.  So the placeholders that matter are the ones outside
    the sections listed above -- and the whole chain is walked, since a consumer
    inherits the parent's fields along with its own.
    """
    hits = []
    for pom in published_pom.chain():
        where = os.path.basename(pom.path) if pom.path else "<memory>"
        for path, value in _consumer_facing_text(pom):
            for found in sorted(set(re.findall(r"\$\{[^}]+\}", value))):
                hits.append("%s %s: %s" % (where, path, found))
    assert not hits, (
        "the published POM leaves properties unexpanded in fields a consumer "
        "resolves:\n  %s" % "\n  ".join(hits))


# ------------------------------------------------------------ sources jar --
@pytest.mark.behavioural
def test_sources_jar_contains_every_main_source(published_sources):
    """All of :gson's main sources, and no more.

    The count is derived from State A's inventory rather than typed, so it states
    the requirement -- every main source root, the generated one included --
    instead of a number that happens to match.
    """
    javas = [e for e in published_sources.entries if e.endswith(".java")]
    assert len(javas) == JAVA_SOURCE_COUNT, (
        "the sources jar holds %d .java file(s), expected %d: gson's main "
        "sources plus the generated GsonBuildConfig.\n"
        "A sources jar wired to src/main/java alone gets %d."
        % (len(javas), JAVA_SOURCE_COUNT, JAVA_SOURCE_COUNT - 1))


@pytest.mark.behavioural
def test_sources_jar_contains_the_generated_source(published_sources):
    """The filtered ``GsonBuildConfig`` is source too, and it is generated.

    A sources jar whose input is ``src/main/java`` misses exactly the one file
    whose contents the build creates.
    """
    assert published_sources.has(BUILD_CONFIG_SOURCE), (
        "%s is not in the sources jar.  The generated-source directory has to be "
        "part of the sources jar's input, not only of compilation."
        % BUILD_CONFIG_SOURCE)


@pytest.mark.behavioural
def test_sources_jar_generated_source_is_filtered(published_sources):
    """The published source shows the substituted version, not the token."""
    if not published_sources.has(BUILD_CONFIG_SOURCE):
        pytest.fail("%s is not in the sources jar" % BUILD_CONFIG_SOURCE)
    text = published_sources.text(BUILD_CONFIG_SOURCE)
    assert VERSION in text, \
        "GsonBuildConfig.java in the sources jar does not mention %s" % VERSION
    assert "${project.version}" not in text, (
        "GsonBuildConfig.java in the sources jar still contains the raw token; "
        "the template was packaged instead of its filtered output")


@pytest.mark.behavioural
def test_sources_jar_includes_the_module_descriptor_source(published_sources):
    """``module-info.java`` is a main source, so it is in the sources jar.

    State A keeps it at the root of ``src/main/java`` -- moditect reads it from
    there -- and a build that moves it out of the source root to keep javac from
    compiling it at release 7 has to keep it in this jar anyway.
    """
    assert published_sources.has("module-info.java"), (
        "module-info.java is not in the sources jar; it is one of gson's main "
        "sources even though it is not compiled with the rest")


@pytest.mark.behavioural
def test_sources_jar_has_no_class_files(published_sources):
    bad = [e for e in published_sources.entries if e.endswith(".class")]
    assert not bad, (
        "the sources jar contains %d .class file(s): %s\n"
        "Its input is the source sets, not the compiled output."
        % (len(bad), bad[:5]))


@pytest.mark.behavioural
def test_sources_jar_has_no_test_sources(published_sources):
    """Only main sources.  A test source set in the sources jar is a wiring error."""
    bad = [e for e in published_sources.entries
           if e.endswith("Test.java") or e.endswith("TestCase.java")]
    assert not bad, \
        "the sources jar contains test sources: %s" % bad[:5]


@pytest.mark.behavioural
def test_sources_jar_does_not_ship_the_template(published_sources):
    """The template input is not packaged beside its filtered output.

    Two copies of ``GsonBuildConfig.java`` -- one with the token and one without
    -- is what a build that added ``java-templates`` to the source set without
    filtering it produces.
    """
    bad = [e for e in published_sources.entries if "java-templates" in e]
    assert not bad, "the sources jar contains template inputs: %s" % bad[:5]


@pytest.mark.behavioural
def test_sources_jar_covers_every_published_class(published_sources,
                                                  published_jar):
    """Every class in the jar has its source in the sources jar.

    The two artifacts are built from one source set, so a class with no source is
    a sources jar assembled from a different input than the one that was
    compiled.  Nested classes resolve to their outer file, ``package-info``
    resolves to its own, and the release-9 descriptor is exempt: it is compiled
    from ``module-info.java`` at the jar root, which is checked above.
    """
    have = {e for e in published_sources.entries if e.endswith(".java")}
    missing = []
    for entry in published_jar.classes():
        if entry.startswith("META-INF/"):
            continue
        if not _has_source_for(entry, have):
            missing.append(entry)
    assert not missing, (
        "%d class(es) in the published jar have no source in the sources jar: "
        "%s" % (len(missing), sorted(missing)[:8]))


# ------------------------------------------------- only gson is published --
@pytest.mark.behavioural
@pytest.mark.parametrize("project", CONTRACT["unpublished_projects"])
def test_sibling_module_not_published(publish_root, project):
    """extras, metrics and proto are internal; State A ships none of them.

    Applying ``maven-publish`` to every subproject with one convention block is
    the natural way to write this and releases three artifacts that have never
    been released.
    """
    artifact = CONTRACT["projects"][project]["artifact"]
    d = os.path.join(publish_root, _rel(artifact))
    assert not os.path.isdir(d), (
        "%s was published as %s.  State A publishes only %s: extras and metrics "
        "are unreleased, and proto exists for the protobuf adapter's tests."
        % (project, artifact, CONTRACT["published_projects"]))


@pytest.mark.behavioural
def test_parent_pom_publishes_no_code(publish_root):
    """The reactor's root ships no artifact of its own.

    If a parent POM is published it is there so a resolver can read it, which is
    the requirement ``test_pom_parent_is_resolvable`` states from the other end.
    What it must not do is ship code: a jar, a sources jar or a javadoc jar under
    ``gson-parent`` means the aggregator was built as a library, and a consumer
    who depended on it would get an empty artifact at coordinates nobody uses.

    So this checks what is in the directory rather than whether it exists.  State
    A's release publishes ``gson-parent-2.10.1.pom`` and its checksums and nothing
    else; a flattened Gradle publication has no such directory at all, and both
    pass.
    """
    d = os.path.join(publish_root, _rel(CONTRACT["root_project_name"]))
    if not os.path.isdir(d):
        return
    code = []
    for dirpath, _dirnames, filenames in os.walk(d):
        for fn in filenames:
            if fn.endswith((".jar", ".war", ".aar", ".zip")):
                code.append(os.path.relpath(os.path.join(dirpath, fn),
                                            publish_root))
    assert not code, (
        "%s was published with %d artifact(s) of its own: %s\n"
        "An aggregator POM may be published so a consumer can resolve it; it has "
        "no code to ship." % (CONTRACT["root_project_name"], len(code),
                              sorted(code)))


@pytest.mark.behavioural
def test_published_artifact_ids(publish_root):
    """The artifact directories under the group are gson, and at most its parent.

    The set, rather than one absence at a time: a publication that invents a fifth
    artifact under a name none of the checks above name is caught here.

    ``gson-parent`` is admitted because it is not an artifact in the sense this
    check is about.  It carries no code -- asserted directly above -- and it is
    present or absent according to whether the publisher writes flattened POMs,
    which is a property of the tool rather than of what the project releases.
    Stage 3 draws the same line in ``srbgson._is_publisher_bookkeeping``, and the
    two stages disagreeing about it would mean a submission could be told its
    publication was wrong here and right there.
    """
    group_dir = os.path.join(publish_root, *GROUP.split("."))
    if not os.path.isdir(group_dir):
        pytest.fail("nothing published under %s.\nPublished files:\n%s"
                    % (GROUP, _listing(publish_root)))
    got = sorted(d for d in os.listdir(group_dir)
                 if os.path.isdir(os.path.join(group_dir, d)))
    want = sorted(CONTRACT["projects"][p]["artifact"]
                  for p in CONTRACT["published_projects"])
    allowed = sorted(set(want) | {CONTRACT["root_project_name"]})
    assert got in (want, allowed), (
        "published artifacts are %s, want %s (or %s, if the publisher writes a "
        "parent POM)" % (got, want, allowed))


@pytest.mark.behavioural
def test_nothing_published_outside_the_group(publish_root):
    """The repository root holds the group path and nothing else.

    A publication that wrote its own group -- ``org.example``, or the project
    directory name -- puts the artifact at coordinates no consumer looks at.
    """
    top = GROUP.split(".")[0]
    got = sorted(d for d in os.listdir(publish_root)
                 if os.path.isdir(os.path.join(publish_root, d)))
    assert got == [top], (
        "the publication root holds %s, want only %r.\nPublished files:\n%s"
        % (got, top, _listing(publish_root)))


# ------------------------------------------ the published jar is the jar --
@pytest.mark.audit
def test_published_jar_is_the_built_jar(b_publish, published_jar):
    """Byte-identical to the jar this build produced.

    ``maven-publish`` publishes an artifact file; it does not re-assemble one.  A
    publication wired to a task that builds its own copy can diverge from the
    artifact the rest of this suite measured -- different manifest, missing
    multi-release entry -- and then every check above is describing a file nobody
    downloads.
    """
    built = b_publish.primary_jar("gson")
    if built is None:
        pytest.fail(
            "the publish configuration produced no jar under gson/build; the "
            "publication has nothing of the build's to publish.\n%s"
            % ("" if b_publish.ok else b_publish.failure_summary()))
    with open(published_jar.path, "rb") as fh:
        published_bytes = fh.read()
    with open(built, "rb") as fh:
        built_bytes = fh.read()
    assert published_bytes == built_bytes, (
        "the published gson-%s.jar is not the jar this build produced "
        "(%s, %d bytes vs %d bytes).\n"
        "A publication publishes the built artifact; one that assembles its own "
        "copy ships something the tests never saw."
        % (VERSION, os.path.basename(built), len(published_bytes),
           len(built_bytes)))


@pytest.mark.behavioural
def test_published_jar_entry_set_matches_the_assembled_jar(published_jar, jars):
    """And it holds what ``assemble`` produced, entry for entry.

    Stated across configurations as well as within one: the check above says the
    publication did not rebuild, and this says the tree it published from is the
    tree the 282 entry checks were made against.
    """
    built = jars.require("gson")
    assert published_jar.entries == built.entries, (
        "the published jar's entry set differs from the assembled jar's.\n"
        "only in published: %s\nonly in assembled: %s"
        % (sorted(set(published_jar.entries) - set(built.entries))[:8],
           sorted(set(built.entries) - set(published_jar.entries))[:8]))


@pytest.mark.behavioural
def test_published_jar_is_an_osgi_bundle(published_jar):
    """The bnd-generated manifest survives publication."""
    got = published_jar.manifest.get("Bundle-SymbolicName")
    assert got == "com.google.gson", (
        "the published jar's Bundle-SymbolicName is %r; the publication is "
        "shipping a plain jar instead of the bundle the build produced" % got)


@pytest.mark.behavioural
def test_published_jar_carries_the_module_descriptor(published_jar):
    """And the multi-release descriptor with it.

    The descriptor is added to the jar after it is assembled, so a publication
    ordered before that step publishes a jar that is not a module.
    """
    assert published_jar.has(DESCRIPTOR), (
        "the published jar has no %s; on a module path it would be an automatic "
        "module named by its file name" % DESCRIPTOR)


@pytest.mark.behavioural
def test_published_jar_is_multi_release(published_jar):
    """``Multi-Release: true`` reached the consumer's copy.

    Without the header the JVM ignores ``META-INF/versions/`` entirely, so the
    descriptor beside it is inert -- a jar that carries both and says neither is
    the failure this catches.
    """
    got = published_jar.manifest.get("Multi-Release")
    assert (got or "").lower() == "true", \
        "the published jar's Multi-Release header is %r, want 'true'" % got


@pytest.mark.behavioural
def test_published_sources_jar_is_the_built_sources_jar(b_publish,
                                                        published_sources):
    """The sources jar is published from the build's output too.

    Same claim as for the jar, made where a build that generates its sources jar
    inside the publication block rather than as a task would show a difference.
    """
    built = b_publish.jar("gson", name="gson-%s-sources.jar" % VERSION)
    if built is None:
        pytest.skip("the build produced no sources jar under gson/build; only "
                    "the published copy can be read")
    with open(published_sources.path, "rb") as fh:
        a = fh.read()
    with open(built, "rb") as fh:
        b = fh.read()
    assert a == b, (
        "the published gson-%s-sources.jar differs from the one the build "
        "produced" % VERSION)


@pytest.mark.behavioural
def test_published_checksums_match_their_files(publish_root):
    """Any checksum Gradle wrote beside an artifact is correct.

    Gradle writes ``.md5``/``.sha1``/``.sha256``/``.sha512`` sidecars into a file
    repository, and a consumer verifies them before using the download.  Publishing
    a stale sidecar beside a rebuilt artifact makes the artifact unusable while
    every other check here passes.
    """
    import hashlib
    algos = {".md5": hashlib.md5, ".sha1": hashlib.sha1,
             ".sha256": hashlib.sha256, ".sha512": hashlib.sha512}
    checked, bad = 0, []
    d = os.path.join(publish_root, _rel(ARTIFACT, VERSION))
    if not os.path.isdir(d):
        pytest.skip("no %s directory to inspect" % _rel(ARTIFACT, VERSION))
    for fn in sorted(os.listdir(d)):
        stem, ext = os.path.splitext(fn)
        if ext not in algos or not os.path.isfile(os.path.join(d, stem)):
            continue
        with open(os.path.join(d, fn)) as fh:
            want = fh.read().strip().split()[0].lower()
        with open(os.path.join(d, stem), "rb") as fh:
            got = algos[ext](fh.read()).hexdigest()
        checked += 1
        if got != want:
            bad.append("%s says %s, %s hashes to %s" % (fn, want, stem, got))
    if not checked:
        pytest.skip("the publication wrote no checksum sidecars")
    assert not bad, "published checksums do not match their artifacts: %s" % bad
