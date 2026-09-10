#!/usr/bin/env python3
"""Bytecode equivalence, class by class.

State A compiles main sources with ``--release 7`` and gson's own tests with
``--release 17``, and produces a Java 9 module descriptor for the multi-release
jar.  That is three different target levels in one reactor, and getting one wrong
is invisible until a consumer on an old JVM fails to load a class.

javac is deterministic for a fixed compiler version and fixed options, and this
image ships the same Temurin 17 the agent's does.  So for 271 of State A's 277
packaged classes the check is exact: the same source compiled with the same options
must produce the same bytes.  Where State A's bytes are *not* reproducible by a
different build system, the class is checked semantically instead -- see the
`jpms` module for the module descriptor, and ``semantic_only`` below for the
annotation-free ``package-info`` classes, whose constant-pool ordering differs
between Maven's and Gradle's javac invocation while their content does not.
"""
import json
import os

import pytest

DATA = os.path.join(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"), "data")
with open(os.path.join(DATA, "classes.json")) as _fh:
    CLASSES = json.load(_fh)

EXACT_CASES = []       # (key, entry, sha256)
MAJOR_CASES = []       # (key, entry, major)
SEMANTIC_CASES = []    # (key, entry, major)
for _key in sorted(CLASSES):
    for _entry, _info in sorted(CLASSES[_key]["exact"].items()):
        EXACT_CASES.append((_key, _entry, _info["sha256"]))
        MAJOR_CASES.append((_key, _entry, _info["major"]))
    for _entry, _info in sorted(CLASSES[_key].get("semantic_only", {}).items()):
        SEMANTIC_CASES.append((_key, _entry, _info["major"]))


# ---------------------------------------------------------------- byte identity
@pytest.mark.behavioural
@pytest.mark.parametrize("key,entry,want", EXACT_CASES,
                         ids=["%s:%s" % (k, e) for k, e, _ in EXACT_CASES])
def test_class_bytes_identical(jars, key, entry, want):
    """This class compiles to exactly State A's bytes."""
    jar = jars.require(key)
    assert jar.has(entry), "%s is missing from %s" % (
        entry, os.path.basename(jar.path))
    got = jar.sha256(entry)
    assert got == want, (
        "%s differs from State A.\n"
        "Same source, same JDK -- so the compile options differ: check --release, "
        "-encoding, -parameters, -g and whether the module descriptor is being "
        "compiled into the main output.\n"
        "  State A: %s\n  produced: %s" % (entry, want, got))


# -------------------------------------------------------------- class-file level
@pytest.mark.behavioural
@pytest.mark.parametrize("key,entry,want", MAJOR_CASES,
                         ids=["%s:%s" % (k, e) for k, e, _ in MAJOR_CASES])
def test_class_major_version(jars, key, entry, want):
    """Target bytecode level, per class.

    51 = Java 7 (``maven.compiler.release`` in the parent POM),
    53 = Java 9 (the module descriptor),
    61 = Java 17 (gson's test compilation, which never reaches a jar).
    """
    jar = jars.require(key)
    assert jar.has(entry), "%s is missing" % entry
    got = jar.major(entry)
    assert got == want, (
        "%s targets class-file major %d, State A targets %d (Java %d).\n"
        "A jar whose classes target a newer JVM than State A's silently drops "
        "support for the platforms gson 2.10.1 supports."
        % (entry, got, want, want - 44))


@pytest.mark.behavioural
@pytest.mark.parametrize("key,entry,want", SEMANTIC_CASES,
                         ids=["%s:%s" % (k, e) for k, e, _ in SEMANTIC_CASES])
def test_semantic_only_class_present_and_targeted(jars, key, entry, want):
    """State A's bytes are not reproducible here, so check what is contractual.

    These ``package-info.class`` files carry no annotations, so their content is a
    name and a source-file attribute; Maven's and Gradle's javac invocations order
    the constant pool differently.  Presence and target level are the contract;
    the exact bytes are not.
    """
    jar = jars.require(key)
    assert jar.has(entry), (
        "%s is missing. javac only emits package-info.class for an "
        "annotation-free package-info.java when asked (-Xpkginfo:always); "
        "State A's jar contains all of them." % entry)
    assert jar.major(entry) == want, \
        "%s targets major %d, want %d" % (entry, jar.major(entry), want)


# -------------------------------------------------------------- aggregate shape
@pytest.mark.behavioural
@pytest.mark.parametrize("key", sorted(CLASSES))
def test_class_count(jars, key):
    jar = jars.require(key)
    want = len(CLASSES[key]["exact"]) + len(CLASSES[key].get("semantic_only", {}))
    got = len(jar.classes())
    assert got == want, "%s has %d classes, State A has %d" % (
        os.path.basename(jar.path), got, want)


@pytest.mark.behavioural
@pytest.mark.parametrize("key", sorted(CLASSES))
def test_major_version_histogram(jars, key):
    """The distribution of target levels, as a single readable check."""
    jar = jars.require(key)
    want = {int(k): v for k, v in CLASSES[key]["major_histogram"].items()}
    got = {}
    for entry in jar.classes():
        m = jar.major(entry)
        got[m] = got.get(m, 0) + 1
    assert got == want, (
        "%s bytecode target distribution is %s, State A's is %s"
        % (os.path.basename(jar.path), got, want))


@pytest.mark.behavioural
def test_no_class_targets_above_state_a(jars):
    """Nothing anywhere targets a newer JVM than State A's highest (Java 9)."""
    worst = {}
    for key in sorted(CLASSES):
        jar = jars.require(key)
        for entry in jar.classes():
            m = jar.major(entry)
            if m > 53:
                worst["%s:%s" % (key, entry)] = m
    assert not worst, (
        "classes target above Java 9: %s\n"
        "The main sources must compile with --release 7; only the module "
        "descriptor may be Java 9." % dict(list(worst.items())[:8]))


@pytest.mark.audit
def test_main_classes_target_java7(jars):
    """Every class outside META-INF/versions is Java 7, as State A's are.

    This is the single most consequential compile setting in the migration:
    Gradle's default is the toolchain's own level (17), so a build that does not
    set ``options.release = 7`` produces a jar that will not load on the platforms
    gson 2.10.1 supports -- and it looks fine until it is deployed.
    """
    bad = {}
    for key in sorted(CLASSES):
        jar = jars.require(key)
        for entry in jar.classes():
            if entry.startswith("META-INF/versions/"):
                continue
            m = jar.major(entry)
            if m != 51:
                bad["%s:%s" % (key, entry)] = m
    assert not bad, (
        "%d class(es) do not target Java 7: %s"
        % (len(bad), dict(list(bad.items())[:8])))


@pytest.mark.behavioural
@pytest.mark.parametrize("key", sorted(CLASSES))
def test_bytes_identical_under_a_second_build(jars, rebuild_jars, key):
    """The same sources built twice produce the same class bytes.

    Not a repeat of the checks above: those compare against State A, this
    compares the submission against itself.  A build that embeds a timestamp, a
    hostname or a path into its class files passes every exact check when the
    ground truth happens to match and fails here, which is the honest place for
    "this build is not reproducible" to appear.
    """
    a = jars.require(key)
    b = rebuild_jars.require(key)
    differing = sorted(e for e in a.classes()
                       if b.has(e) and a.sha256(e) != b.sha256(e))
    assert not differing, (
        "%d class(es) in %s differ between two builds of the same sources: %s"
        % (len(differing), os.path.basename(a.path), differing[:8]))


@pytest.mark.behavioural
@pytest.mark.parametrize("key", sorted(CLASSES))
def test_bytes_identical_from_a_different_directory(jars, relocated_jars, key):
    """The same sources at a deeper path produce the same class bytes.

    The companion to the check above, with the variable changed from *when* the
    build ran to *where* it ran.  A build script that resolves an input against an
    absolute path, or against its own depth below the root, still succeeds from the
    directory it was written in; the difference only appears when the tree moves,
    and it appears as different output rather than as an error.

    A class file records the *base name* of its source and no directory, so a
    correct build produces identical bytes here even though every path involved
    changed.
    """
    a = jars.require(key)
    b = relocated_jars.require(key)
    differing = sorted(e for e in a.classes()
                       if b.has(e) and a.sha256(e) != b.sha256(e))
    assert not differing, (
        "%d class(es) in %s differ when the same sources are built from a deeper "
        "directory: %s\nSomething in the build depends on where the tree sits."
        % (len(differing), os.path.basename(a.path), differing[:8]))


@pytest.mark.behavioural
@pytest.mark.parametrize("key", sorted(CLASSES))
def test_class_set_complete_from_a_different_directory(jars, relocated_jars, key):
    """And the relocated build produced all of them.

    Read separately from the byte comparison above, which only compares entries
    both jars have: a build whose input paths are absolute can silently compile
    nothing for one source set, and that failure is an absence rather than a
    difference.
    """
    a = jars.require(key)
    b = relocated_jars.require(key)
    missing = sorted(set(a.classes()) - set(b.classes()))
    assert not missing, (
        "%d class(es) present in the default build are missing from the same "
        "sources built at a deeper path: %s"
        % (len(missing), missing[:8]))
