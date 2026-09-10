"""Who constructs the handlers and hands them their collaborators?

The second half of the dispatch question, and the one a submission is most likely
to get half-right in a way no other stage can see.

State A's handlers do not construct anything.  ``RouteResource`` takes a
``GraphHopperConfig``, a ``GraphHopper``, a ``ProfileResolver`` and a
``TranslationMap`` on an ``@Inject`` constructor, and HK2 supplies all four from
the ``AbstractBinder`` the bundle registers -- fifteen ``@Inject`` constructors in
``src/main``, thirteen bindings in one binder, and exactly one resource in the whole
tree built with ``new`` (``RootResource``, which has no dependencies).  The
migration's real work is moving those thirteen bindings into whatever Spring calls
them, and the tempting shortcut is not to: keep the resource classes, delete the
annotations, and have one dispatcher construct each of them with ``new`` and a
static field for the ``GraphHopper``.

That shortcut answers every request correctly.  Stage 2 measures response bytes and
sees nothing; a graph is a graph however it got there.  What it costs is the
property the task is about -- the container owning the object graph -- and it costs
it invisibly, which is why these checks exist here and are worth a reviewer's turns.

The checks look for the two shapes the shortcut takes.  A handler that builds its
own collaborators, and a static holder that passes them around instead of a
container.  Both are silent on State A: no ``getInstance()``, no static mutable
singleton, no resource built with ``new`` other than the one, and every
collaborator arriving through an injection point.

Advisory, like everything in this stage.  Constructor injection is not mandatory
and a Spring application may legitimately use ``new`` for a value object; the
finding is a place to look.
"""

from __future__ import annotations

import re

import pytest

import srbscan

pytestmark = pytest.mark.scan

#: Injection-point annotations either stack understands.  ``@Inject`` is
#: ``jakarta.inject`` and survives the migration -- it is a specification
#: annotation, not Dropwizard's -- so a submission may keep it and let Spring honour
#: it, and both spellings count as the container doing the work.
INJECTION_ANNOTATIONS = ("Inject", "Autowired", "Resource", "Bean", "Value",
                         "Qualifier", "Named", "ConfigurationProperties",
                         "ConstructorBinding")

#: Class-level annotations that put a type under the container's management on
#: either stack.
MANAGED_ANNOTATIONS = (set(srbscan.SPRING_CLASS_ANNOTATIONS)
                       | {"Provider", "Singleton", "Path", "Bean",
                          "EnableAutoConfiguration", "ComponentScan"})


def _handler_files(tree) -> dict[str, dict[str, list[int]]]:
    """The files that declare a request handler, by either vocabulary."""
    return srbscan.mappings(tree)


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------

def test_report_how_collaborators_reach_the_handlers(repo):
    """REPORT. Injection points, and which handler classes have one.

    The single most informative number in this module: how many of the classes
    that declare handlers also declare an injection point.  State A: 13 of 14 --
    the sole exception is ``RootResource``, which needs nothing and is also the one
    resource the bootstrap builds with ``new``.  A port where that ratio collapsed
    to zero has handlers that get their collaborators some other way, and the next
    check says which way.
    """
    handlers = _handler_files(repo)
    injected: dict[str, list[str]] = {}
    for rel in handlers:
        path = repo / rel
        uses = srbscan.annotation_uses(path)
        got = [f"@{name}" for name in INJECTION_ANNOTATIONS if name in uses]
        if got:
            injected[rel] = got

    total_points = 0
    for path, rel in srbscan.java_sources(repo):
        uses = srbscan.annotation_uses(path)
        total_points += sum(len(uses.get(name, ())) for name in INJECTION_ANNOTATIONS)

    pytest.fail(
        f"REPORT (not a defect): {len(injected)} of {len(handlers)} handler-"
        f"declaring class(es) carry an injection-point annotation; "
        f"{total_points} injection-point annotation(s) in the delivered sources "
        f"overall. State A: 12 of 14, and 15 @Inject constructors.\n"
        + "\n".join(f"  {rel}: {', '.join(injected[rel])}"
                    for rel in sorted(injected)[:20])
        + ("\n  (further classes not listed)" if len(injected) > 20 else "")
        + "\n  handler classes with NO injection point: "
        + f"{sorted(set(handlers) - set(injected))[:8]}"
    )


def test_report_where_the_object_graph_is_declared(repo):
    """REPORT. The file that holds the bindings, whatever it is called now.

    State A has one: ``GraphHopperBundle`` binds thirteen types inside a single
    ``AbstractBinder``.  A Spring port typically has one or two ``@Configuration``
    classes with ``@Bean`` methods, or it has component scanning and no such file at
    all -- both are correct, and which one it is changes what the reviewer should
    read next.  A port with neither has its object graph somewhere unusual.
    """
    candidates: dict[str, list[str]] = {}
    markers = ("AbstractBinder", "bindFactory(", "bind(", "@Bean",
               "@Configuration", "@ComponentScan", "registerBean(",
               "BeanDefinition", "@Import")
    for path, rel in srbscan.java_sources(repo):
        text = srbscan.read(path)
        got = [m for m in markers if m in text]
        # `bind(` alone is too common to be a marker: it matches ByteBuffer and
        # socket code.  Require two independent markers, or one of the specific
        # ones.
        specific = [m for m in got if m not in ("bind(",)]
        if len(specific) >= 1:
            candidates[rel] = got

    if not candidates:
        pytest.fail(
            "REPORT: no file in the delivered sources declares bindings in any "
            "recognised form -- no AbstractBinder, no @Configuration, no @Bean, no "
            "@ComponentScan, no registerBean. The object graph is built somewhere "
            "this scan does not recognise; find it before answering the wiring "
            "gate.")

    ranked = sorted(candidates.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    pytest.fail(
        f"REPORT (not a defect): {len(candidates)} file(s) declare bindings. "
        f"State A: one, GraphHopperBundle, with 13 bindings in one AbstractBinder.\n"
        + "\n".join(f"  {rel}: {', '.join(markers_found)}"
                    for rel, markers_found in ranked[:14])
    )


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def test_no_handler_constructs_its_own_collaborators(repo):
    """A handler that calls ``new`` on a service is a handler outside the container.

    Restricted to the handler classes and to the types State A's binder supplies,
    because ``new ArrayList<>()`` inside a handler is not a wiring decision.  The
    thirteen bound types are the object graph the migration has to move; a handler
    that builds one itself has moved it into the handler.

    Silent on State A: every one of those types arrives through an ``@Inject``
    constructor, and the only ``new`` on a bound type in the whole tree is the
    bundle building the graph it then binds.
    """
    bound = ("GraphHopper(", "GraphHopperConfig(", "GraphHopperManaged(",
             "ProfileResolver(", "TranslationMap(", "LocationIndex(",
             "EncodingManager(", "BaseGraph(", "GtfsStorage(",
             "JTSTriangulator(", "PathDetailsBuilderFactory(",
             "GHRequestTransformer(", "RealtimeFeed(")
    findings: list[str] = []
    for rel in sorted(_handler_files(repo)):
        path = repo / rel
        for lineno, line in enumerate(srbscan.read(path).splitlines(), 1):
            if srbscan._COMMENT_LINE.match(line):
                continue
            code = line.split("//", 1)[0]
            for t in bound:
                if "new " + t in code:
                    findings.append(f"  {rel}:{lineno}: {code.strip()[:120]}")
    assert not findings, (
        f"{len(findings)} site(s) inside handler classes construct a type the "
        f"container is supposed to supply. Every one of these is bound in State A's "
        f"single binder; a handler that builds one has taken it out of the object "
        f"graph:\n" + "\n".join(findings[:16])
    )


def test_no_static_holder_stands_in_for_the_container(repo):
    """The other half of the shortcut: a global instead of an injection.

    ``private static GraphHopper HOPPER`` plus a setter called from ``main`` is a
    complete replacement for dependency injection, it is four lines, and no later
    stage can see it -- the responses are identical.  What it costs is the container
    owning the lifecycle, which is the property the wiring gate is about.

    Silent on State A: no ``getInstance()``, no ``INSTANCE`` field, and no
    non-final static field of a bound type anywhere in ``src/main``.  Generated
    protobuf code under ``vector_tile`` is exempt -- it is full of static factory
    methods and it is not code anyone wrote.
    """
    bound = {"GraphHopper", "GraphHopperConfig", "ProfileResolver",
             "TranslationMap", "LocationIndex", "EncodingManager", "BaseGraph",
             "GtfsStorage", "Triangulator", "ApplicationContext"}
    holder = re.compile(
        r'\b(?:private|protected|public)?\s*static\s+(?!final\b)([\w.<>\[\]]+)\s+(\w+)\s*[=;]')
    findings: list[str] = []
    for path, rel in srbscan.java_sources(repo):
        if "vector_tile" in rel:
            continue
        text = srbscan.read(path)
        for lineno, line in enumerate(text.splitlines(), 1):
            if srbscan._COMMENT_LINE.match(line):
                continue
            m = holder.search(line.split("//", 1)[0])
            # The declared type has to BE a bound type, not merely contain one's
            # name.  A substring test was tried and matched
            # `static GtfsStorage.EdgeType[] edgeTypeValues` in reader-gtfs, which
            # is a cached `values()` array and not a service at all -- a nested type
            # of a bound type is not the bound type, and neither is an array of one.
            if m and m.group(1).split("<", 1)[0] in bound:
                findings.append(f"  {rel}:{lineno}: static {m.group(1)} "
                                f"{m.group(2)}")
        if "getInstance()" in text:
            findings.extend("  " + c
                            for c in srbscan.cite(path, rel, "getInstance()", limit=2))
    assert not findings, (
        f"{len(findings)} static holder(s) of a type the container supplies. A "
        f"static field plus a setter is a working substitute for injection that "
        f"answers every request correctly and takes the lifecycle away from the "
        f"container:\n" + "\n".join(findings[:16])
    )


def test_the_handlers_are_still_managed_types(repo):
    """Something has to tell the container these classes exist.

    On State A that is ``jersey().register(RouteResource.class)``; on a Spring port
    it is ``@RestController``, ``@Component``, a ``@Bean`` method, or a scan that
    picks the package up.  A handler class with no class-level management annotation
    AND no mention in any binding file is a class the container does not know about
    -- which means something else is instantiating it.

    Silent on State A: all fourteen carry ``@Path`` and all fourteen are named at the
    bootstrap.  Both halves are checked because either alone is legitimate: a Spring
    ``@RestController`` needs no registration line, and a class registered by
    ``registerBean`` needs no annotation.
    """
    handlers = _handler_files(repo)
    if not handlers:
        pytest.skip("no handler-declaring class found; the dispatch module reports "
                    "that, and this check has nothing to measure")

    # Everything any delivered source file mentions by simple name, so a class
    # registered in a configuration file counts as known.
    mentioned: set[str] = set()
    for path, rel in srbscan.java_sources(repo):
        if rel in handlers:
            continue
        text = srbscan.read(path)
        for name in (r.rsplit("/", 1)[-1][:-5] for r in handlers):
            if name in text:
                mentioned.add(name)

    orphans: list[str] = []
    for rel, uses in sorted(handlers.items()):
        simple = rel.rsplit("/", 1)[-1][:-5]
        managed = [n for n in uses if n in MANAGED_ANNOTATIONS]
        if not managed and simple not in mentioned:
            orphans.append(f"  {rel}: no class-level management annotation "
                           f"({sorted(uses)}) and no other source file names it")
    assert not orphans, (
        f"{len(orphans)} handler-declaring class(es) are neither annotated as "
        f"managed nor named anywhere else in the delivered sources. Whatever "
        f"instantiates them, it is not the container:\n" + "\n".join(orphans[:12])
    )


def test_report_the_lifecycle_mechanism(repo):
    """REPORT. The graph has to be loaded before the first request and closed after the last.

    State A does this with ``environment.lifecycle().manage(graphHopperManaged)``:
    ``GraphHopperManaged`` implements Dropwizard's ``Managed``, so ``start`` imports
    or loads the graph and ``stop`` closes it.  Spring's equivalents are
    ``InitializingBean``/``DisposableBean``, ``@PostConstruct``/``@PreDestroy``,
    ``SmartLifecycle``, or a ``@Bean(destroyMethod=...)``.

    A submission that dropped the hook entirely still passes stage 2 -- the graph
    gets loaded lazily on first use, or in a static initialiser, and the responses
    match.  What it loses is the ordered shutdown, and a routing graph that is not
    closed leaves its memory-mapped files behind.  So this reports rather than
    judges: WHICH mechanism the port chose, listed for the reviewer, because there
    are six legitimate answers on Spring and no way to rank them from outside.
    """
    hooks = ("Managed", "InitializingBean", "DisposableBean", "PostConstruct",
             "PreDestroy", "SmartLifecycle", "Lifecycle", "destroyMethod",
             "initMethod", "ApplicationRunner", "CommandLineRunner",
             "ContextClosedEvent", "ApplicationListener", "AutoCloseable",
             "Closeable")
    found: dict[str, list[str]] = {}
    for path, rel in srbscan.java_sources(repo):
        text = srbscan.read(path)
        got = [h for h in hooks if h in text]
        if got:
            found[rel] = got
    pytest.fail(
        f"REPORT (not a defect): {len(found)} file(s) reference a lifecycle "
        f"mechanism. State A manages the graph through Dropwizard's Managed in "
        f"GraphHopperManaged and RealtimeFeedLoadingCache; a port needs some "
        f"equivalent, and which one it chose is worth knowing before reading the "
        f"wiring gate.\n"
        + "\n".join(f"  {rel}: {', '.join(found[rel][:5])}"
                    for rel in sorted(found)[:16])
        + ("\n  (further files not listed)" if len(found) > 16 else "")
    )


def test_the_configuration_object_still_arrives_from_a_file(repo, original):
    """Both stacks are configured by a YAML file, and the file is part of the CLI.

    State A takes ``server config-example.yml`` on the command line and binds the
    parsed ``GraphHopperConfig`` into the container; the behavioural stage launches
    both jars with a config file and will not accept a jar that ignores it, so a
    submission cannot hardcode its settings and pass.  What it CAN do is read the
    file itself with a YAML parser and skip the container, which works and is not
    what either framework does.

    Differential on the config surface: which files in the tree parse YAML
    directly.  State A does it in exactly the places Dropwizard's own bootstrap
    does, so a new site is a new decision.
    """
    def yaml_readers(tree):
        markers = ("new Yaml(", "Yaml(", "YAMLFactory", "ObjectMapper(new YAML",
                   "readValue(new File", "loadAs(", "SnakeYaml")
        out: dict[str, list[str]] = {}
        for path, rel in srbscan.java_sources(tree):
            text = srbscan.read(path)
            got = [m for m in markers if m in text]
            if got:
                out[rel] = got
        return out

    before, after = yaml_readers(original), yaml_readers(repo)
    added = {rel: v for rel, v in after.items() if rel not in before}
    assert not added, (
        f"{len(added)} file(s) parse YAML directly where State A did not. The "
        f"config object is supposed to reach the handlers through the container; a "
        f"handler that reads the file itself has bypassed it:\n"
        + "\n".join(f"  {rel}: {', '.join(v)}" for rel, v in sorted(added.items()))
    )
