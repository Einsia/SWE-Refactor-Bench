#!/usr/bin/env python3
"""The version is one input, and everything derived from it follows.

In State A the version lives in exactly one place -- the parent POM's
``<version>`` -- and Maven propagates it to the jar names, the OSGi
``Bundle-Version``, every ``Export-Package`` version, the module descriptor's
version, the published POM and its file names, and into ``GsonBuildConfig.java``
through resource filtering.

Rebuilding all of that with ``2.10.1`` typed in six places produces a build that
is correct today and wrong at the next release, and no check on a single build can
tell the two apart.  So the whole build runs again with an arbitrary version
supplied on the command line and the same artefacts are inspected.  Anything still
saying 2.10.1 was hardcoded.

7.11.3 is unlike 2.10.1 in every component, and is not a free choice: gson's own
``GsonVersionDiagnosticsTest`` asserts that the version it finds at runtime matches
``\\d\\.\\d+\\.\\d``, so a probe version with a two-digit patch would fail gson's
suite under a perfectly correct migration.  Any replacement must keep a
single-digit major and patch.

This module reads the probe build's artefacts.  Whether the version is declared
once or six times in the build scripts is a question about code, asked in stage 1
by reading it; here the same property is measured by its consequences, which is
the only form in which a submission cannot argue with it.
"""
import json
import os
import re

import pytest

import classfile
import jarinspect

DATA = os.path.join(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"), "data")
with open(os.path.join(DATA, "version_probe.json")) as _fh:
    PROBE = json.load(_fh)
with open(os.path.join(DATA, "build_contract.json")) as _fh:
    CONTRACT = json.load(_fh)
with open(os.path.join(DATA, "manifest.json")) as _fh:
    MF = json.load(_fh)

V = PROBE["probe_version"]              # 7.11.3
OLD = CONTRACT["version"]               # 2.10.1
GROUP = CONTRACT["group"]
BUILD_CONFIG = PROBE["build_config_class"]
DESCRIPTOR = "META-INF/versions/9/module-info.class"

#: (project, expected jar name) under -Pversion=7.11.3.  proto is the exception:
#: State A pins its finalName, so it stays gson-proto.jar.
JAR_CASES = sorted((p.lstrip(":"), os.path.basename(n))
                   for p, n in PROBE["jars"].items())

#: The manifest headers whose value is derived from the version.  Each is a
#: separate check: bnd takes Bundle-Version from the project and the export
#: versions from the bundle version, so a build that pins one and derives the
#: other is a real and quite common half-migration.
VERSIONED_HEADERS = ["Bundle-Version"]

#: Headers that must NOT change with the version.  Bundle-SCM is the interesting
#: one -- see test_scm_tag_is_not_a_version below.
STABLE_HEADERS = ["Bundle-SymbolicName", "Bundle-Name", "Bundle-Description",
                  "Bundle-Vendor", "Bundle-ContactAddress",
                  "Bundle-RequiredExecutionEnvironment", "Require-Capability",
                  "Import-Package", "Multi-Release", "Bundle-ManifestVersion",
                  "Manifest-Version"]

PUBLISHED_FILES = ["gson-%s.jar" % V, "gson-%s.pom" % V,
                   "gson-%s-sources.jar" % V]


# ----------------------------------------------------------------- the build --
@pytest.mark.audit
def test_version_override_build_succeeds(b_version):
    """``-Pversion=7.11.3 build publish`` completes.

    ``-Pversion`` is the documented override in the build contract; a build that
    fails under it cannot be released.
    """
    assert b_version.ok, b_version.failure_summary()


# -------------------------------------------------------------- jar naming ---
@pytest.mark.behavioural
@pytest.mark.parametrize("project,want", JAR_CASES,
                         ids=[p for p, _ in JAR_CASES])
def test_jar_name_follows_version(b_version, project, want):
    """This project's jar is named for the supplied version."""
    found = [os.path.basename(p) for p in b_version.libs(project)]
    assert want in found, (
        "under -Pversion=%s, %s produced %s; expected %s.\n%s"
        % (V, project, found or "nothing", want,
           "" if b_version.ok else b_version.failure_summary()))


@pytest.mark.behavioural
def test_no_stale_version_in_jar_names(b_version):
    """No artefact still carries 2.10.1 in its name."""
    stale = sorted({os.path.basename(p)
                    for project in ("gson", "extras", "metrics", "proto")
                    for p in b_version.libs(project)
                    if OLD in os.path.basename(p)})
    assert not stale, (
        "these artefacts still carry the hardcoded version %s: %s" % (OLD, stale))


@pytest.mark.behavioural
def test_proto_jar_name_does_not_follow_version(b_version):
    """gson-proto.jar keeps its fixed name.

    State A pins ``<finalName>`` for this one project, so a build that versions
    all four names uniformly is wrong in a way the other checks would not notice.
    """
    found = [os.path.basename(p) for p in b_version.libs("proto")]
    assert "gson-proto.jar" in found, (
        "the proto project produced %s under -Pversion=%s; State A pins its "
        "name to gson-proto.jar regardless of version" % (found or "nothing", V))


# ------------------------------------------------------- generated source ----
@pytest.mark.audit
def test_build_config_carries_supplied_version(version_jars):
    """``GsonBuildConfig.VERSION`` is the supplied version.

    The filtering step, measured at the one point where a pre-substituted copy
    and a filtered template become distinguishable: with a different version
    supplied, only the filtered one changes.
    """
    jar = version_jars.require("gson")
    assert jar.has(BUILD_CONFIG), "%s missing from the jar" % BUILD_CONFIG
    strings = classfile.ClassFile(jar.read(BUILD_CONFIG)).strings()
    assert V in strings, (
        "GsonBuildConfig does not contain %r under -Pversion=%s.\n"
        "Version-shaped constants found: %s\n"
        "If %s appears instead, the version was substituted when the file was "
        "written rather than when the build ran."
        % (V, V, [s for s in strings if re.match(r"^\d+\.\d+", s)][:5], OLD))


@pytest.mark.audit
def test_build_config_does_not_carry_the_old_version(version_jars):
    """And it does not still contain 2.10.1 beside it."""
    jar = version_jars.require("gson")
    strings = classfile.ClassFile(jar.read(BUILD_CONFIG)).strings()
    assert OLD not in strings, \
        "GsonBuildConfig still contains the hardcoded %s" % OLD


@pytest.mark.behavioural
def test_no_template_directory_in_jar(version_jars):
    """The template input is not shipped beside its filtered output."""
    jar = version_jars.require("gson")
    bad = [e for e in jar.entries if "java-templates" in e]
    assert not bad, "the jar contains template inputs: %s" % bad[:5]


# ---------------------------------------------------------- OSGi metadata ----
@pytest.mark.behavioural
@pytest.mark.parametrize("header", VERSIONED_HEADERS)
def test_versioned_header_follows_supplied_version(version_jars, header):
    """This manifest header tracks the project version.

    An OSGi container resolves by version range, so a bundle that always claims
    2.10.1 cannot be upgraded by the framework that is supposed to manage it.
    """
    mf = version_jars.require("gson").manifest
    got = mf.get(header)
    want = PROBE["manifest"][header]
    assert got == want, (
        "%s is %r under -Pversion=%s, want %r" % (header, got, V, want))


@pytest.mark.behavioural
def test_export_package_versions_follow_supplied_version(version_jars):
    """Every exported package is versioned with the supplied version.

    bnd takes the export version from the bundle version unless the instructions
    pin it, so a submission that wrote ``version=2.10.1`` into its bnd
    instructions passes every default-build check and fails here.
    """
    mf = version_jars.require("gson").manifest
    clauses = jarinspect.parse_osgi_header(mf.get("Export-Package", ""))
    got = sorted(attrs.get("version") for attrs in clauses.values())
    want = sorted(PROBE["manifest"]["Export-Package-versions"])
    assert got == want, (
        "Export-Package versions are %s under -Pversion=%s, want %s"
        % (got, V, want))


@pytest.mark.behavioural
def test_no_stale_version_in_manifest(version_jars):
    """No manifest header still says 2.10.1.

    ``Bundle-SCM``'s ``tag`` is exempt: it names the git tag the sources came
    from (``gson-parent-2.10.1``), which State A takes from a literal
    ``<scm><tag>`` in the parent POM.  Maven does not substitute the build
    version into it either, so a faithful migration keeps it as it is.
    """
    mf = version_jars.require("gson").manifest
    stale = []
    for k, v in mf.items():
        v = v or ""
        if k == "Bundle-SCM":
            v = re.sub(r'tag\s*=\s*"[^"]*"', "", v)
        if OLD in v:
            stale.append(k)
    assert not sorted(stale), (
        "manifest headers still carrying the hardcoded %s: %s"
        % (OLD, sorted(stale)))


@pytest.mark.behavioural
@pytest.mark.parametrize("header", STABLE_HEADERS)
def test_stable_header_is_unchanged_by_the_version(version_jars, jars, header):
    """This header is the same in both builds.

    The complement of the checks above: a build that interpolates the version
    into ``Bundle-Name`` or narrows ``Import-Package`` when the version changes is
    deriving something from an input it should not depend on.
    """
    a = version_jars.require("gson").manifest.get(header)
    b = jars.require("gson").manifest.get(header)
    assert a == b, (
        "%s differs between the default build and -Pversion=%s:\n"
        "  default: %s\n  probe  : %s" % (header, V, b, a))


@pytest.mark.behavioural
def test_scm_tag_is_not_a_version(version_jars):
    """``Bundle-SCM``'s tag still names the upstream tag.

    Stated positively so the exemption above is visible as a requirement rather
    than as a hole: the header must still be there, and must still carry the tag
    State A recorded.
    """
    got = version_jars.require("gson").manifest.get("Bundle-SCM") or ""
    want = MF["provenance"]["Bundle-SCM"]
    m = re.search(r'tag\s*=\s*"([^"]*)"', want)
    if not m:
        pytest.skip("State A's Bundle-SCM carries no tag attribute")
    tag = m.group(1)
    assert tag in got, (
        "Bundle-SCM no longer names the upstream tag %r under -Pversion=%s: %s"
        % (tag, V, got))


# ------------------------------------------------------ module descriptor ----
@pytest.mark.behavioural
def test_module_version_follows_supplied_version(version_jars):
    """The descriptor's recorded version follows too.

    ``--module-version`` has to be handed the project's version rather than a
    literal, and a descriptor generated without it records no version at all.
    """
    jar = version_jars.require("gson")
    assert jar.has(DESCRIPTOR), "%s missing under the version probe" % DESCRIPTOR
    mod = classfile.ClassFile(jar.read(DESCRIPTOR)).parse().module()
    assert mod is not None, "%s is not a module descriptor" % DESCRIPTOR
    assert mod["version"] == PROBE["module_version"], (
        "the module descriptor records version %r under -Pversion=%s, want %r"
        % (mod["version"], V, PROBE["module_version"]))


@pytest.mark.behavioural
def test_module_name_is_unchanged_by_the_version(version_jars, jars):
    """The module's *name* does not follow the version.

    A descriptor generated with the version folded into the automatic-module
    naming rules renames the module at every release, which breaks every
    ``requires`` clause downstream.
    """
    a = classfile.ClassFile(
        version_jars.require("gson").read(DESCRIPTOR)).parse().module()
    b = classfile.ClassFile(jars.require("gson").read(DESCRIPTOR)).parse().module()
    assert a["name"] == b["name"], (
        "the module is named %r under -Pversion=%s and %r by default"
        % (a["name"], V, b["name"]))


# ------------------------------------------------------------ publication ----
@pytest.mark.behavioural
@pytest.mark.parametrize("filename", PUBLISHED_FILES)
def test_published_file_follows_version(b_version, filename):
    """This published file is named for the supplied version."""
    p = b_version.published("gson", filename, version=V)
    assert p is not None, (
        "no %s published under %s/gson/%s/ with -Pversion=%s"
        % (filename, GROUP.replace(".", "/"), V, V))


@pytest.mark.behavioural
def test_published_pom_coordinates_follow_version(b_version):
    """The published POM's coordinates, as a consumer resolves them, follow it.

    Read through the parent chain rather than off this file, because ``groupId``
    and ``version`` are the two coordinates Maven inherits: a publication that
    writes a thin POM beside its parent declares neither locally, and both resolve
    correctly anyway.  ``artifactId`` is never inherited, so it is read directly.
    """
    want = PROBE["pom"]
    p = b_version.published("gson", "gson-%s.pom" % V, version=V)
    assert p is not None, "no gson-%s.pom published" % V
    pom = jarinspect.Pom(p, repo_root=b_version.publish_root())
    group, artifact, version = pom.coordinates()
    assert version == want["version"], \
        "published version resolves to %r, want %r" % (version, want["version"])
    assert artifact == want["artifactId"], \
        "published artifactId is %r, want %r" % (artifact, want["artifactId"])
    assert group == want["groupId"], \
        "published groupId resolves to %r, want %r" % (group, want["groupId"])


@pytest.mark.behavioural
def test_no_stale_version_directory_published(b_version):
    """Nothing was published under 2.10.1 during the probe."""
    root = b_version.publish_root()
    if root is None:
        pytest.fail("the version probe published nothing.\n\n%s"
                    % b_version.failure_summary())
    stale = [os.path.relpath(os.path.join(dirpath, d), root)
             for dirpath, dirnames, _fn in os.walk(root)
             for d in dirnames if d == OLD]
    assert not stale, (
        "the publication used the hardcoded version %s: %s" % (OLD, stale))


@pytest.mark.behavioural
def test_published_jar_is_the_built_jar(b_version):
    """The published jar is the one the probe build produced.

    Byte-identical, not merely same-named: a publication assembled from a
    different source than the build's own output is how a stale artefact reaches
    a repository.
    """
    published = b_version.published("gson", "gson-%s.jar" % V, version=V)
    built = b_version.primary_jar("gson")
    if published is None or built is None:
        pytest.fail("published=%r built=%r under -Pversion=%s"
                    % (published, built, V))
    assert open(published, "rb").read() == open(built, "rb").read(), (
        "the published gson-%s.jar differs from the jar the same build produced"
        % V)


# ------------------------------------------------- the version changes nothing else
@pytest.mark.behavioural
def test_jar_entry_set_unchanged_by_the_version(version_jars, jars):
    """Changing the version changes versions, and nothing else.

    Same classes, same resources.  A difference means the version is feeding
    something it should not, such as a path inside the jar.
    """
    a = version_jars.require("gson")
    b = jars.require("gson")
    assert a.entries == b.entries, (
        "the jar's contents differ between -Pversion=%s and the default build.\n"
        "only in probe: %s\nonly in default: %s"
        % (V, sorted(set(a.entries) - set(b.entries))[:8],
           sorted(set(b.entries) - set(a.entries))[:8]))


@pytest.mark.behavioural
@pytest.mark.parametrize("key", ["gson", "gson-extras", "gson-metrics",
                                 "gson-proto"])
def test_class_bytes_change_only_where_the_version_appears(version_jars, jars,
                                                           key):
    """A class that changed with the version changed only in the version.

    Naming the classes that may differ would be wrong, and measurably so:
    ``GsonBuildConfig.VERSION`` is a ``static final String``, so javac inlines its
    value into the constant pool of every class that references it -- ``Gson`` and
    ``ReflectionHelper`` in State A, whichever classes upstream chooses to
    reference it tomorrow -- and the multi-release ``module-info`` carries the
    module version besides.  A fixed list would be a list of which sources happen
    to mention a constant, which is upstream's business and not the build's.

    So the property is stated directly.  For every class whose bytes moved, each
    string constant that appears in one build and not the other must contain that
    build's version.  A build that stamps the version into unrelated classes, or
    that recompiles something differently between the two runs, has a string that
    changed for some other reason and fails here.
    """
    a = version_jars.require(key)
    b = jars.require(key)
    differing = sorted(n for n in set(a.classes()) & set(b.classes())
                       if a.sha256(n) != b.sha256(n))
    unexplained = {}
    for name in differing:
        # Every UTF-8 entry, not only the String constants: a module
        # descriptor's version is a bare Utf8 reached from the Module attribute
        # and never a CONSTANT_String, so reading only the literals would report
        # module-info as "bytes differ, no string changed".
        sa = set(classfile.ClassFile(a.read(name)).utf8s())
        sb = set(classfile.ClassFile(b.read(name)).utf8s())
        # Every string unique to the probe build must carry the probe version,
        # and every string unique to the default build the default one.  What is
        # left over is a change the version does not explain.
        stray = ([s for s in sorted(sa - sb) if V not in s]
                 + [s for s in sorted(sb - sa) if OLD not in s])
        if stray or sa == sb:
            unexplained[name] = stray[:4] or ["bytes differ, no string changed"]
    assert not unexplained, (
        "%d class(es) in %s changed for a reason other than the version:\n%s"
        % (len(unexplained), key,
           "\n".join("  %s: %s" % (n, s)
                     for n, s in sorted(unexplained.items())[:6])))


@pytest.mark.behavioural
def test_build_config_did_change(version_jars, jars):
    """And the one that should change did.

    The positive half of the check above.  If GsonBuildConfig is byte-identical
    across two different versions, the version reaching it is a constant.
    """
    a = version_jars.require("gson")
    b = jars.require("gson")
    assert a.sha256(BUILD_CONFIG) != b.sha256(BUILD_CONFIG), (
        "GsonBuildConfig.class is identical under -Pversion=%s and the default "
        "build; the version is not reaching it" % V)


@pytest.mark.behavioural
@pytest.mark.parametrize("key", ["gson-extras", "gson-metrics", "gson-proto"])
def test_sibling_jar_entry_set_unchanged_by_the_version(version_jars, jars, key):
    """The other three jars' contents do not depend on the version either."""
    a = version_jars.require(key)
    b = jars.require(key)
    assert a.entries == b.entries, (
        "%s's contents differ between -Pversion=%s and the default build:\n"
        "only in probe: %s\nonly in default: %s"
        % (key, V, sorted(set(a.entries) - set(b.entries))[:6],
           sorted(set(b.entries) - set(a.entries))[:6]))
