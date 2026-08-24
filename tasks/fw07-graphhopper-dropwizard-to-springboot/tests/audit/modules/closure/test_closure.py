"""Is the retired stack gone from what the compiler and the resolver read?

Every check here is advisory.  None can fail the task on its own: they are read
into the reviewer's prompt as ``{{findings}}`` and the reviewer decides what, if
anything, they mean.  That division carries more weight in this module than
anywhere else in the ladder, because "no Dropwizard left" is not a mechanical
property of this repository.

The retired surface is annotations and one registration list.  ``@Path`` on a
class, ``@GET`` on a method, ``environment.jersey().register(RouteResource.class)``
at the bootstrap.  A submission can declare ``@interface Path`` in its own package,
write a servlet that reflects over it, delete every ``jakarta.ws.rs`` import, and
satisfy every assertion below while the dispatch is still JAX-RS-shaped and Spring
is doing nothing but hosting a servlet.  Conversely a submission can trip several
of these and be a clean port: a comment reading ``// Dropwizard applied this
filter before the resource; Spring's filter order is the same`` names the retired
stack in order to explain the code that replaced it, and that comment is the one a
careful migration writes.

So the checks are ordered by how much of the answer each can carry.  Import blocks
first, parsed rather than grepped, because the import block is what javac reads and
a file that does not import ``io.dropwizard`` cannot construct it.  Then the poms,
because a dependency that is not declared is not on the classpath at all, and
because the BOM import is how this repository pulls the whole stack in with one
line.  Then two derived comparisons against State A -- did the reactor keep its
modules, did the coordinates actually change -- because a submission that deleted
a module has not migrated it.  Then the shape checks, which look for the retired
stack rebuilt under new names, because a framework that was re-implemented imports
nothing.  The token sweep is last and weakest: it reads text the compiler may never
see.

Calibration.  Every check in this file that names the retired stack FIRES on State
A, by construction -- State A is the Dropwizard application.  That is the opposite
of the other three modules in this suite, which are silent on it, and it is why
this module's findings say "still" rather than "unexpectedly": the reviewer is
reading them as a progress report on a migration, not as an alarm.
"""

from __future__ import annotations

import pytest

import srbscan

pytestmark = pytest.mark.scan

GROUPS = srbscan.retired_groups()
PACKAGES = srbscan.retired_packages()

#: The types that make State A a Dropwizard application rather than a program that
#: happens to depend on one.  Named individually because the import-prefix checks
#: above report a package and the reviewer's question is about a contract: a class
#: that still ``extends Application`` has kept the retired framework's entry point
#: even if the import was rewritten to a submission-local class of the same name.
FRAMEWORK_CONTRACTS = (
    "Application", "ConfiguredBundle", "Bootstrap", "Environment",
    "AbstractBinder", "ResourceConfig", "ConfiguredCommand", "Managed",
    "HealthCheck", "AssetsBundle",
)


# --------------------------------------------------------------------------
# what the compiler reads
# --------------------------------------------------------------------------

@pytest.mark.parametrize("package", PACKAGES)
def test_no_delivered_source_imports_a_retired_package(repo, package):
    """The strongest single observation in the file.

    ``srbscan.imports`` parses the import block, so this counts what javac
    resolves and nothing else -- not a mention in a comment, not a ``{@link}`` in a
    Javadoc, not a package name inside a string.  A ``src/main`` file that imports
    this still compiles against the retired stack.

    Measured counts on State A, so a reviewer can read the number as progress:
    ``jakarta.ws.rs`` 20 files, ``io.dropwizard`` 15, ``org.glassfish.hk2`` 3,
    ``com.codahale.metrics`` 2, ``org.glassfish.jersey`` 1, and zero for
    ``org.jvnet.hk2`` and ``com.fasterxml.jackson.jakarta.rs`` -- the last two are
    listed because the artifacts are on the classpath, and a submission that starts
    naming them directly is going the wrong way.
    """
    hits = srbscan.importers(repo, package, tests=False)
    assert not hits, (
        f"{len(hits)} delivered source file(s) still import {package}:\n"
        + "\n".join(f"  {rel}: {', '.join(hits[rel][:4])}"
                    for rel in sorted(hits))
    )


@pytest.mark.parametrize("package", PACKAGES)
def test_no_test_file_imports_a_retired_package(repo, package):
    """Split from the check above because the two findings mean different things.

    A handler that still imports the retired stack is a port that did not happen.
    A *test* that still imports it is a test left behind -- and it is a leftover no
    later stage reports, because the behavioural stage builds both sides against a
    local repository that carries the complete closure (it has to: the reference it
    builds IS State A) and would compile that test happily.

    The count is large on State A and mostly one type: ``DropwizardAppExtension``
    is the JUnit 5 extension that boots the application for 23 test classes.  A
    submission that ported the application and kept those tests has 23 files that
    will not compile, so this check tends to be the one that tells the reviewer
    which half of the work was done.
    """
    all_hits = srbscan.importers(repo, package, tests=True)
    src_hits = srbscan.importers(repo, package, tests=False)
    hits = {rel: v for rel, v in all_hits.items() if rel not in src_hits}
    assert not hits, (
        f"{len(hits)} test file(s) still import {package}:\n"
        + "\n".join(f"  {rel}: {', '.join(hits[rel][:4])}"
                    for rel in sorted(hits))
    )


# --------------------------------------------------------------------------
# what the resolver reads
# --------------------------------------------------------------------------

@pytest.mark.parametrize("group", GROUPS)
def test_no_pom_declares_a_retired_group(repo, group):
    """A dependency that is not declared is not on the classpath.

    Every ``<dependency>`` in the tree, wherever it sits -- direct,
    ``dependencyManagement``, or inside a plugin -- because all three put the
    retired stack back within reach and a reviewer asking "is it still declared"
    means any of them.  Scope is reported because it changes the reading: a
    ``test``-scoped one is a stale test dependency, and the check above will have
    named the test.
    """
    hits: list[str] = []
    for path, rel in srbscan.poms(repo):
        for dep in srbscan.pom_dependencies(path):
            if dep["group"] == group or dep["group"].startswith(group + "."):
                scope = dep["scope"] or "compile"
                line = srbscan.locate(path, dep["artifact"])
                where = f"{rel}:{line}" if line else rel
                hits.append(f"  {where}: {dep['coord']} (scope {scope})")
    assert not hits, (
        f"{len(hits)} dependency declaration(s) still name group {group}:\n"
        + "\n".join(sorted(hits))
    )


def test_no_pom_imports_the_retired_stacks_bom(repo):
    """One line brings the whole stack's versions back.

    State A's root pom imports ``io.dropwizard:dropwizard-dependencies`` as a BOM.
    A submission that removed every direct dependency and kept that import has kept
    the version of every artifact in the retired stack available to any module that
    asks for one without a version -- and it is the single edit most likely to be
    forgotten, because removing it is not required to make the build pass.

    Reported separately from the group check above, which also matches it, because
    the reviewer's reading differs: a leftover BOM import with no remaining
    dependencies is housekeeping, and a leftover BOM import with dependencies under
    it is the stack.
    """
    hits: list[str] = []
    for path, rel in srbscan.poms(repo):
        for bom in srbscan.pom_managed_imports(path):
            group = bom["group"]
            if any(group == g or group.startswith(g + ".") for g in GROUPS):
                line = srbscan.locate(path, bom["artifact"])
                hits.append(f"  {rel}:{line or '?'}: imports BOM {bom['coord']}")
    assert not hits, "retired-stack BOM still imported:\n" + "\n".join(sorted(hits))


def test_the_reactor_still_builds_every_module_it_did(repo, original):
    """A module that was deleted was not migrated.

    Differential, and it has to be: the number of modules is not a property a
    reviewer can know from the submission alone.  If ``web`` or ``navigation``
    disappeared from the reactor, the behavioural stage would still build a jar and
    still compare it -- against a reference that has the code, so the comparison
    would run and report missing endpoints as behavioural differences.  This check
    is what lets the reviewer say the difference was structural.

    A module ADDED is not reported.  A migration may reasonably split configuration
    or add a starter module, and the count going up says nothing by itself.
    """
    def modules_of(tree):
        out: dict[str, list[str]] = {}
        for path, rel in srbscan.poms(tree):
            mods = srbscan.pom_modules(path)
            if mods:
                out[rel] = mods
        return out

    before, after = modules_of(original), modules_of(repo)
    lost: list[str] = []
    for rel, mods in sorted(before.items()):
        now = set(after.get(rel, []))
        for m in mods:
            if m not in now:
                lost.append(f"  {rel}: no longer declares <module>{m}</module>")
    assert not lost, (
        f"{len(lost)} reactor module(s) declared by State A are not declared by "
        f"the submission:\n" + "\n".join(lost)
    )


def test_the_dependency_set_changed_at_all(repo, original):
    """The cheapest way to see whether the poms were touched.

    A migration of this size cannot leave the coordinate set identical: Spring
    Boot's starters have to arrive from somewhere.  If this check passes -- that is,
    if it finds no difference -- the submission's build inputs are State A's, and
    whatever else changed, the framework did not.

    Written as an assertion so it appears in the findings the same way the others
    do, and inverted: it FAILS when there is a difference, which is the expected
    and healthy outcome, and it PASSES silently on an untouched tree.  The reviewer
    is told which reading is which in the message.
    """
    before = set(srbscan.declared_coords(original))
    after = set(srbscan.declared_coords(repo))
    added, removed = sorted(after - before), sorted(before - after)
    assert not (added or removed), (
        f"the declared dependency set differs from State A's, which is what a "
        f"migration looks like -- this is a description, not a defect. "
        f"{len(added)} added, {len(removed)} removed.\n"
        + "\n".join(f"  + {c}" for c in added[:25])
        + ("\n  + (further additions not listed)" if len(added) > 25 else "")
        + "\n"
        + "\n".join(f"  - {c}" for c in removed[:25])
        + ("\n  - (further removals not listed)" if len(removed) > 25 else "")
    )


# --------------------------------------------------------------------------
# the retired stack rebuilt under other names
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", FRAMEWORK_CONTRACTS)
def test_no_type_still_implements_a_retired_framework_contract(repo, name):
    """A renamed framework imports nothing, so the imports cannot find it.

    ``extends Application<GraphHopperServerConfiguration>`` and
    ``implements ConfiguredBundle`` are the two declarations that make State A's
    entry point and its bundle what they are.  A submission that wrote its own
    ``Application`` interface and kept the class hierarchy has a tree with no
    retired imports and the retired architecture; a submission that moved to
    ``@SpringBootApplication`` has neither.

    ``HealthCheck`` and ``Managed`` are on the list and they are the weakest
    entries: a submission may legitimately name its own lifecycle interface
    ``Managed``.  That is what the citation is for.
    """
    hits: list[str] = []
    for path, rel in srbscan.java_sources(repo):
        if name in srbscan.extends_or_implements(path):
            line = srbscan.locate(path, name)
            hits.append(f"  {rel}:{line or '?'}: extends/implements {name}")
    assert not hits, (
        f"{len(hits)} delivered type(s) still declare the retired framework's "
        f"{name} contract:\n" + "\n".join(sorted(hits))
    )


def test_no_dispatch_annotation_is_declared_inside_the_tree(repo):
    """The cheat this whole module is arranged around.

    State A declares no annotation types at all -- 0 ``@interface`` in 935 Java
    files -- and it uses ``@Path``, ``@GET``, ``@POST``, ``@Produces`` and
    ``@Consumes`` from ``jakarta.ws.rs``.  A submission that declares any of those
    names itself has kept the vocabulary and supplied its own implementation, and
    every import check above goes quiet at the same moment.  The same is true in the
    other direction: a submission that declares ``@RestController`` or
    ``@GetMapping`` in its own package is not using Spring's, it is imitating it.

    This does not prove a hand-rolled framework: a submission may declare an
    annotation for its own reasons and let Spring dispatch. The reviewer has the
    file.
    """
    watched = (set(srbscan.JAXRS_METHOD_ANNOTATIONS)
               | set(srbscan.SPRING_MAPPING_ANNOTATIONS)
               | set(srbscan.SPRING_CLASS_ANNOTATIONS)
               | {"Path", "Produces", "Consumes", "QueryParam", "PathParam",
                  "DefaultValue", "Provider", "Context", "BeanParam"})
    decls = srbscan.annotation_declarations(repo)
    hits = {name: where for name, where in decls.items() if name in watched}
    assert not hits, (
        "the tree declares annotation types whose names belong to one of the two "
        "web stacks; a submission that supplies its own @Path or its own "
        "@GetMapping is imitating a framework rather than using one:\n"
        + "\n".join(f"  @{name}: {', '.join(where)}"
                    for name, where in sorted(hits.items()))
    )


# --------------------------------------------------------------------------
# the token sweep, last and weakest
# --------------------------------------------------------------------------

#: Names that only mean something inside the retired stack.  Deliberately short
#: and specific: each is a symbol State A calls, not a word that describes it.
#: ``Dropwizard`` and ``Jersey`` as bare words are NOT here -- they appear in this
#: repository's own CHANGELOG, README and Javadoc, and prose is exempt from the
#: sweep anyway, but a comment inside a .java file is not.
_RETIRED_CALLS = (
    "jersey().register(", "environment.jersey()", "environment.lifecycle()",
    "environment.healthChecks()", "environment.servlets()",
    "bootstrap.addBundle(", "bootstrap.addCommand(", "AbstractBinder",
    "run(args)", "ConfiguredCommand", "SimpleServerFactory",
    "DefaultServerFactory", "HttpConnectorFactory", "JerseyViolationException",
)


@pytest.mark.parametrize("token", _RETIRED_CALLS)
def test_no_delivered_source_calls_the_retired_api(repo, token):
    """Text, not types -- so a hit is a pointer, not a conclusion.

    This catches the two cases the import checks miss: a call through a
    fully-qualified name with no import, and a call on a type the submission
    re-declared locally.  It also catches a comment, and it will: ``// Dropwizard
    registered these on environment.jersey()`` trips it.  That is acceptable at the
    bottom of the file, where the reviewer has already read the import graph and the
    poms and knows what it is looking at.

    ``run(args)`` is the loosest entry and it is here because ``new
    GraphHopperApplication().run(args)`` is State A's ``main``; a submission whose
    main still reads that way has kept the entry point whatever it renamed.
    """
    hits: dict[str, list[str]] = {}
    for path, rel in srbscan.java_sources(repo):
        if token in srbscan.read(path):
            hits[rel] = srbscan.cite(path, rel, token, limit=3)
    assert not hits, (
        f"{token!r} appears in {len(hits)} delivered source file(s):\n"
        + "\n".join(line for rel in sorted(hits) for line in hits[rel])
    )
