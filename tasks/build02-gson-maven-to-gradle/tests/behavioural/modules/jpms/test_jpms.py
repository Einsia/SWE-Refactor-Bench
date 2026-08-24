#!/usr/bin/env python3
"""What the module descriptor says.

``gson/src/main/java/module-info.java`` declares a module that exports four
packages and requires two things optionally.  Compiling it is the subtlest step of
this migration -- release 7 cannot express a module, so the descriptor needs its
own compilation, and its output belongs under ``META-INF/versions/9/`` so a Java 7
consumer still sees a plain jar.  That the file exists in the right place is the
`derive` module's question.  This module asks what it *means*.

The distinction matters because the bytes are not comparable.  State A's
descriptor is synthesised by moditect with ASM and is 305 bytes carrying a single
``Module`` attribute; the same source compiled by javac is 345 bytes and carries
``ModulePackages`` beside it.  Both are correct, both resolve identically, and a
byte comparison would reject the correct one.  So the descriptor is decoded and
its declarations are compared: the module's name, its recorded version, its
exports and their targets, its requires and their modifiers, and the three lists
that must stay empty.

Every value here comes from ``module-info.java``, which the submission may not
edit.  A descriptor that disagrees with it was not compiled from it -- it was
written by hand, or generated from something else, or generated with defaults that
happen to differ.

Two things a descriptor cannot tell you are checked elsewhere and deliberately not
repeated: whether the JVM actually resolves the module, and which packages it can
see at run time, are the `runtime` module's five probes, asked by loading the jar
on a module path.  A declaration and a resolution are different claims.
"""
import json
import os

import pytest

import classfile

DATA = os.path.join(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"), "data")
with open(os.path.join(DATA, "module_info.json")) as _fh:
    MI = json.load(_fh)

ENTRY = MI["entry"]
MODULE = MI["module"]
EXPORTS = MODULE["exports"]
REQUIRES = MODULE["requires"]

#: (module name, modifier, expected) -- one check per flag rather than one per
#: dependency.  ``requires static`` is the whole point of both of gson's
#: dependencies: java.sql and jdk.unsupported are optional, and a descriptor that
#: drops ``static`` makes them mandatory, so gson stops resolving on any JVM
#: without them.  A check that folded both flags together would report that as one
#: failure alongside a missing ``transitive``, which is a different mistake.
FLAG_CASES = [(r["name"], flag, r[flag])
              for r in REQUIRES for flag in ("static", "transitive")]

#: The packages in the jar that module-info.java does not export.  Exporting one
#: of these is not a build error and not a test failure; it is a permanent
#: addition to gson's public API surface.
INTERNAL_PREFIXES = ("com.google.gson.internal",)


@pytest.fixture(scope="session")
def descriptor(gson_jar):
    """The decoded ``Module`` attribute, with the class file around it."""
    if not gson_jar.has(ENTRY):
        pytest.fail(
            "%s is missing.\n"
            "State A's jar is a multi-release jar carrying a Java 9 module "
            "descriptor.  gson/src/main/java/module-info.java is the source; a "
            "release-7 compilation cannot accept it, so it needs its own compile "
            "step at release 9 with its output under META-INF/versions/9/."
            % ENTRY)
    data = gson_jar.read(ENTRY)
    cf = classfile.ClassFile(data).parse()
    mod = cf.module()
    if mod is None:
        pytest.fail(
            "%s carries no Module attribute, so it is not a module descriptor "
            "(%d bytes, attributes: %s).  A class named module-info that is an "
            "ordinary class leaves the jar an automatic module."
            % (ENTRY, len(data), sorted(cf.attributes)))
    return {"cf": cf, "mod": mod, "size": len(data)}


# ------------------------------------------------------------- the class file --
@pytest.mark.audit
def test_descriptor_is_a_module_descriptor(descriptor):
    """It decodes, and it carries a ``Module`` attribute.

    Read first: every check below reports a detail of a structure this one says
    exists.
    """
    assert descriptor["mod"]["name"], "the Module attribute names no module"


@pytest.mark.behavioural
def test_descriptor_class_file_version(descriptor):
    """major 53 = Java 9, not the toolchain's own 61.

    The entry lives under ``versions/9``, and a JVM only looks there if it is at
    least 9 -- but a class compiled at 17 in that directory is unreadable on Java
    9 through 16, which is most of the range the directory exists to serve.
    Compiling the descriptor at the toolchain's level is the natural mistake and
    produces a jar that works on the machine that built it.
    """
    got = descriptor["cf"].major
    assert got == MI["major"], (
        "the module descriptor targets class-file major %d, want %d (Java 9)"
        % (got, MI["major"]))


@pytest.mark.behavioural
def test_descriptor_this_class(descriptor):
    """The class is named ``module-info``.

    Not ``com.google.gson.module-info``, and not a placeholder: the name is how
    the JVM recognises the file as a descriptor rather than as a class that
    happens to sit beside one.
    """
    got = descriptor["cf"].this_class
    assert got == MI["this_class"], \
        "the descriptor's class name is %r, want %r" % (got, MI["this_class"])


# ---------------------------------------------------------------- the module --
@pytest.mark.audit
def test_module_name(descriptor):
    """``com.google.gson``.

    The name every consumer's ``requires`` clause spells.  A descriptor generated
    from the jar's file name instead of from the source gets ``gson`` and silently
    breaks every module-path consumer.
    """
    want = MODULE["name"]
    got = descriptor["mod"]["name"]
    assert got == want, "the module is named %r, want %r" % (got, want)


@pytest.mark.behavioural
def test_module_version(descriptor):
    """The descriptor records the module version.

    javac writes it only when handed ``--module-version``; moditect takes it from
    the POM.  A descriptor without one still resolves, so this is easy to lose and
    invisible afterwards -- which is why it is checked here rather than left to a
    runtime probe, where the absence looks like a null.
    """
    want = MODULE["version"]
    got = descriptor["mod"]["version"]
    assert got == want, (
        "the module records version %r, want %r.  Pass --module-version to the "
        "compilation that produces the descriptor." % (got, want))


@pytest.mark.behavioural
def test_module_is_not_open(descriptor):
    """``module``, not ``open module``.

    An open module grants deep reflective access to every package it contains,
    including ``com.google.gson.internal``.  It is a one-word change to the
    descriptor and it removes the encapsulation the module system exists to give.
    """
    assert not descriptor["mod"]["open"], (
        "the module is declared open; State A's is not, and an open module exposes "
        "every internal package to reflection")


# ----------------------------------------------------------------- exports ----
@pytest.mark.behavioural
@pytest.mark.parametrize("exp", EXPORTS, ids=[e["package"] for e in EXPORTS])
def test_module_exports(descriptor, exp):
    """This package is exported, and unqualified.

    ``exports x to y`` is a different declaration from ``exports x``: it makes the
    package readable by one named module and by nothing else, so a consumer that
    is not on the list gets an ``IllegalAccessError`` at the first call.
    """
    got = {e["package"]: e for e in descriptor["mod"]["exports"]}
    assert exp["package"] in got, (
        "the module does not export %s.  module-info.java is unchanged and names "
        "it, so a missing export means the descriptor was not compiled from it.\n"
        "Exported instead: %s" % (exp["package"], sorted(got)))
    assert got[exp["package"]]["to"] == exp["to"], (
        "%s is exported to %s; State A exports it to %s (empty = to everyone)"
        % (exp["package"], got[exp["package"]]["to"], exp["to"]))


@pytest.mark.behavioural
def test_module_exports_exactly(descriptor):
    """The four, and no fifth.

    The set, stated once: a descriptor generated from "every package in the jar"
    exports nine, which is how ``com.google.gson.internal`` becomes public API
    without anyone deciding to make it public.
    """
    got = sorted(e["package"] for e in descriptor["mod"]["exports"])
    want = sorted(e["package"] for e in EXPORTS)
    assert got == want, "the module exports %s, want %s" % (got, want)


@pytest.mark.behavioural
def test_module_exports_no_internal_package(descriptor):
    """And specifically not the internals.

    The same property as above, named.  It is the failure that matters most of the
    ones that check would catch, and a submission that hits it should be told what
    it did rather than shown two lists to diff.
    """
    leaked = sorted(e["package"] for e in descriptor["mod"]["exports"]
                    if e["package"].startswith(INTERNAL_PREFIXES))
    assert not leaked, (
        "the module exports internal package(s): %s\n"
        "gson's internals are deliberately unexported; exporting them makes them "
        "part of the library's public API." % leaked)


@pytest.mark.behavioural
def test_every_exported_package_is_in_the_jar(descriptor, gson_jar):
    """A package cannot be exported and absent.

    The JVM rejects a module whose descriptor exports a package it does not
    contain, at resolution, before any of the library's code runs.  A descriptor
    written by hand against the wrong source tree fails exactly here.
    """
    have = {os.path.dirname(e).replace("/", ".")
            for e in gson_jar.classes() if not e.startswith("META-INF/")}
    missing = sorted(e["package"] for e in descriptor["mod"]["exports"]
                     if e["package"] not in have)
    assert not missing, (
        "the descriptor exports %s, which the jar does not contain.  A module that "
        "exports an absent package does not resolve." % missing)


# ---------------------------------------------------------------- requires ----
@pytest.mark.behavioural
@pytest.mark.parametrize("req", REQUIRES, ids=[r["name"] for r in REQUIRES])
def test_module_requires(descriptor, req):
    """This dependency is declared."""
    got = {r["name"] for r in descriptor["mod"]["requires"]}
    assert req["name"] in got, (
        "the module does not require %s.\nRequired instead: %s"
        % (req["name"], sorted(got)))


@pytest.mark.behavioural
@pytest.mark.parametrize("name,flag,want", FLAG_CASES,
                         ids=["%s-%s" % (n, f) for n, f, _ in FLAG_CASES])
def test_module_requires_modifier(descriptor, name, flag, want):
    """``requires static java.sql`` stays static.

    Both of gson's dependencies are optional: it reflects on ``java.sql`` types
    when they are there and uses ``sun.misc.Unsafe`` from ``jdk.unsupported`` when
    it can.  Dropping ``static`` makes them mandatory, and gson then fails to
    resolve on a JVM image that does not include them -- which is most jlink
    images, and every Android runtime.  Nothing in a test run on a full JDK
    notices.
    """
    got = {r["name"]: r for r in descriptor["mod"]["requires"]}
    if name not in got:
        pytest.fail("the module does not require %s at all" % name)
    assert got[name][flag] == want, (
        "requires %s: %s is %s, State A has %s"
        % (name, flag, got[name][flag], want))


@pytest.mark.behavioural
def test_module_requires_exactly(descriptor):
    """The three, and nothing else.

    ``java.base`` is implicit in the source and mandated in the descriptor, so it
    is in State A's list too.  A fourth entry means the descriptor was generated
    from the compile classpath rather than from the source: a build that hands the
    module compilation gson's full classpath produces ``requires
    com.google.errorprone.annotations``, and gson then does not resolve without a
    module nobody ships.
    """
    got = sorted(r["name"] for r in descriptor["mod"]["requires"])
    want = sorted(r["name"] for r in REQUIRES)
    assert got == want, "the module requires %s, want %s" % (got, want)


@pytest.mark.behavioural
def test_no_requires_carries_a_version(descriptor):
    """No dependency is pinned to a version.

    ``requires`` records a version only when the compiler is told one, and a
    version recorded there is advisory -- the resolver ignores it -- so a
    descriptor that carries one is describing a build detail as if it were a
    contract.  State A's records none.
    """
    pinned = sorted("%s@%s" % (r["name"], r["version"])
                    for r in descriptor["mod"]["requires"] if r["version"])
    assert not pinned, "requires clauses carry versions: %s" % pinned


# ------------------------------------------------- the lists that stay empty --
@pytest.mark.behavioural
def test_module_opens_nothing(descriptor):
    """No ``opens``.

    gson reflects on *its consumers'* classes, not on its own, so it needs no open
    package.  ``opens com.google.gson.internal`` is a plausible thing to add while
    debugging a reflection failure and it is a permanent hole in the module.
    """
    got = descriptor["mod"]["opens"]
    assert got == MODULE["opens"], \
        "the module opens %s, State A opens %s" % (got, MODULE["opens"])


@pytest.mark.behavioural
def test_module_uses_nothing(descriptor):
    """No ``uses``: gson looks up no service."""
    got = descriptor["mod"]["uses"]
    assert got == MODULE["uses"], \
        "the module declares uses %s, State A declares %s" % (got, MODULE["uses"])


@pytest.mark.behavioural
def test_module_provides_nothing(descriptor):
    """And it publishes no service implementation."""
    got = descriptor["mod"]["provides"]
    assert got == MODULE["provides"], (
        "the module provides %s, State A provides %s"
        % (got, MODULE["provides"]))


# ---------------------------------------------------------- the other three --
@pytest.mark.behavioural
@pytest.mark.parametrize("key", ["gson-extras", "gson-metrics", "gson-proto"])
def test_sibling_jar_has_no_module_descriptor(jars, key):
    """Only gson is a named module.

    The other three are internal artefacts and State A gives none of them a
    descriptor.  A build that applies one module configuration to every subproject
    produces four, and three of them declare modules that have never existed --
    each of which would then have to keep its name forever.
    """
    jar = jars.require(key)
    found = sorted(e for e in jar.entries if e.endswith("module-info.class"))
    assert not found, (
        "%s carries %s; State A gives a module descriptor only to gson"
        % (key, found))


@pytest.mark.behavioural
@pytest.mark.parametrize("key", ["gson-extras", "gson-metrics", "gson-proto"])
def test_sibling_jar_has_nothing_under_versions(jars, key):
    """And none of them has a ``META-INF/versions/`` tree at all.

    The complement of the check above.  Whether these three wrongly *claim*
    ``Multi-Release`` is the `manifest` module's question; this one is about
    content, and a versions directory here means a shared jar configuration
    compiled something for a release none of them targets.
    """
    jar = jars.require(key)
    under = sorted(e for e in jar.entries
                   if e.startswith("META-INF/versions/"))
    assert not under, (
        "%s has %d entr(y/ies) under META-INF/versions/: %s"
        % (key, len(under), under[:5]))


# --------------------------------------------------- the descriptor is stable --
@pytest.mark.behavioural
def test_descriptor_is_identical_under_a_relocated_build(gson_jar,
                                                         relocated_jars):
    """Moving the sources changes nothing about the module.

    The descriptor is the one artefact in this jar produced by a tool invoked with
    explicit paths -- a separate compilation, or moditect's replacement -- so it is
    where an absolute path baked into the build shows up as a difference rather
    than as a failure.
    """
    a = gson_jar
    b = relocated_jars.require("gson")
    if not b.has(ENTRY):
        pytest.fail("the relocated build's jar has no %s" % ENTRY)
    assert a.sha256(ENTRY) == b.sha256(ENTRY), (
        "the module descriptor differs between the default build and the same "
        "sources at a deeper path")


@pytest.mark.behavioural
def test_descriptor_declarations_survive_the_probe_source(descriptor, add_jars):
    """Adding a class to an exported package does not change the module.

    ``m_add`` writes ``com/google/gson/SrbProbeGson.java`` into an exported
    package and ``com/google/gson/srbprobe/SrbProbeInternal.java`` into a package
    that does not exist in State A.  Neither is named in module-info.java, so the
    descriptor's exports must be the same four either way: a build that derives
    them from the packages it finds exports five.
    """
    if not add_jars.require("gson").has(ENTRY):
        pytest.fail("the probe build's jar has no %s" % ENTRY)
    probe = classfile.ClassFile(
        add_jars.require("gson").read(ENTRY)).parse().module()
    if probe is None:
        pytest.fail("the probe build's %s is not a module descriptor" % ENTRY)
    got = sorted(e["package"] for e in probe["exports"])
    want = sorted(e["package"] for e in descriptor["mod"]["exports"])
    assert got == want, (
        "with two probe sources added, the module exports %s; the default build "
        "exports %s.  module-info.java names four packages and neither probe is "
        "in it." % (got, want))
