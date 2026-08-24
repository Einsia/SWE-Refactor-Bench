"""What registers the handlers, and how many are there?

The module that reads the lines the task is about.  Stage 2 proves both jars answer
``GET /route?point=...`` the same way; it cannot tell whether the submission's jar
answered because Spring's ``RequestMappingHandlerMapping`` matched a
``@GetMapping``, or because a single servlet mounted on ``/*`` read
``getRequestURI()`` and ran a ``switch``.  Both produce identical bytes.  Only
somebody reading the code can say which, and this module's job is to put the facts
that distinguish them on one page so the reviewer spends its turns deciding rather
than counting.

Two kinds of check live here and they are labelled differently on purpose.

``test_report_*`` ALWAYS emits.  It is an inventory, not an alarm: the message
opens with ``REPORT (not a defect)`` and the reviewer is told in prompt.txt that
these are the scan describing the tree.  A report that went quiet when the tree got
worse would be useless, so they are written to fail unconditionally and carry their
content in the failure message.

Every other check is silent on State A and stays silent on a faithful port.  They
look for the shapes a hand-rolled dispatcher has and a container-dispatched
application does not: one class carrying every mapping, a table of request-path
literals, a ``switch`` on the request path, a servlet mounted on everything,
reflection over annotations the submission declared itself.  State A trips none of
them -- it has 14 resource classes, 17 method-level annotations, no ``@interface``
declaration anywhere in 935 files, no ``switch`` on a path, and one ``Filter`` that
adds CORS headers.

None of this is scored.  A submission can trip every check below and be correct;
the honest reading of a ``switch`` on the request path is "go and look at
web-bundle/src/main/java/.../Dispatcher.java:88", not "fail".
"""

from __future__ import annotations

import re

import pytest

import srbscan

pytestmark = pytest.mark.scan

#: Every annotation that declares an HTTP handler on either stack, plus ``@Path``.
HANDLER_ANNOTATIONS = (set(srbscan.JAXRS_METHOD_ANNOTATIONS)
                       | set(srbscan.SPRING_MAPPING_ANNOTATIONS)
                       | {"Path"})

#: The method-level ones only.  ``@Path`` sits on classes and on methods and is
#: therefore useless for counting handlers; ``@GET`` and ``@GetMapping`` are one
#: handler each wherever they appear.
METHOD_ANNOTATIONS = (set(srbscan.JAXRS_METHOD_ANNOTATIONS)
                      | set(srbscan.SPRING_MAPPING_ANNOTATIONS)
                      - {"RequestMapping"})


# --------------------------------------------------------------------------
# reports: the inventory
# --------------------------------------------------------------------------

def test_report_the_handler_census(repo):
    """REPORT. Which classes declare handlers, with what vocabulary, and how many.

    The two numbers the ``spring_does_the_dispatching`` gate turns on are here:
    how many method-level handler declarations exist, and how they are distributed
    over classes.  State A reads 17 across 14 classes -- 14 ``@GET`` and 3
    ``@POST`` -- and a faithful port lands near that with Spring's vocabulary
    instead.  One class holding 17 mappings is a legal Spring application and also
    exactly what a port looks like when the resources were flattened into a
    dispatcher; the reviewer needs to see which before reading anything else.
    """
    census = srbscan.mappings(repo)
    if not census:
        pytest.fail(
            "REPORT: no handler-declaring annotation of either vocabulary was "
            "found in the delivered sources. Either dispatch is declared somewhere "
            "this scan does not read, or the request path does not go through an "
            "annotated handler at all -- a servlet, a filter or a behavioural "
            "route. Read the bootstrap.")

    per_class: dict[str, int] = {}
    vocab: dict[str, int] = {}
    for rel, uses in census.items():
        n = sum(len(lines) for name, lines in uses.items()
                if name in METHOD_ANNOTATIONS)
        if n:
            per_class[rel] = n
        for name, lines in uses.items():
            vocab[name] = vocab.get(name, 0) + len(lines)

    total = sum(per_class.values())
    ranked = sorted(per_class.items(), key=lambda kv: (-kv[1], kv[0]))
    pytest.fail(
        f"REPORT (not a defect): {total} method-level handler declaration(s) "
        f"across {len(per_class)} class(es); {len(census)} file(s) carry an "
        f"annotation from either vocabulary. State A: 17 across 14.\n"
        f"  annotations seen: "
        f"{ {k: v for k, v in sorted(vocab.items(), key=lambda kv: -kv[1])} }\n"
        + "\n".join(f"  {n:3d}  {rel}" for rel, n in ranked[:24])
        + ("\n  (further classes not listed)" if len(ranked) > 24 else "")
    )


def test_report_the_bootstrap(repo):
    """REPORT. Every line in the delivered sources that looks like registering.

    Broad and noisy: ``register(`` matches a Jackson module and a metrics registry
    as well as a resource.  What it is for is showing the bootstrap's shape in one
    place, because the bootstrap is where a migration either handed wiring to the
    container or kept an enumerated list and renamed the method it is called from.
    State A shows 24 such lines in ``GraphHopperBundle`` and 3 in
    ``GraphHopperApplication``; a Spring port that has moved to component scanning
    shows very few, and one that kept the list shows about as many as before.
    """
    table: list[str] = []
    per_file: dict[str, int] = {}
    for path, rel in srbscan.java_sources(repo):
        regs = srbscan.registrations(path)
        if regs:
            per_file[rel] = len(regs)
            for lineno, line in regs[:8]:
                table.append(f"  {rel}:{lineno}: {line}")

    if not table:
        pytest.fail(
            "REPORT: no registration-shaped call was found in the delivered "
            "sources at all. On a Spring port that is what full component "
            "scanning looks like, and it is also what a submission with no "
            "wiring looks like. Read the entry point.")

    ranked = sorted(per_file.items(), key=lambda kv: (-kv[1], kv[0]))
    pytest.fail(
        f"REPORT (not a defect): {sum(per_file.values())} registration-shaped "
        f"line(s) in {len(per_file)} file(s). State A: 24 in GraphHopperBundle, "
        f"3 in GraphHopperApplication.\n"
        + "\n".join(f"  {n:3d}  {rel}" for rel, n in ranked[:12]) + "\n"
        + "\n".join(table[:28])
    )


def test_report_where_the_paths_are_written(repo):
    """REPORT. The request paths as the submission spells them.

    State A writes them as ``@Path`` values without a leading slash -- ``route``,
    ``nearest``, ``match``, ``{z}/{x}/{y}.mvt`` -- so a sweep for ``"/..."``
    literals finds almost none of the contract, which is why this check reads the
    annotation arguments instead of grepping for slashes.  A Spring port writes
    them as ``@GetMapping("/route")`` and the same reader picks them up.

    The contract is public, so every honest submission contains these strings and
    their presence is the task rather than a smell.  Twelve class-level prefixes
    take a handler directly -- ``/route`` (GET and POST, the one path with two),
    ``/nearest``, ``/isochrone``, ``/spt``, ``/match``, ``/i18n``, ``/info``,
    ``/health``, ``/navigate``, ``/route-pt``, ``/isochrone-pt`` and ``/`` -- and
    four more endpoints are a class-level prefix plus a method-level sub-path:
    ``/i18n/{locale}``, ``/mvt/{z}/{x}/{y}.mvt``, ``/pt-mvt/{z}/{x}/{y}.mvt`` and
    ``/navigate/directions/v5/gh/{profile}/{coordinatesArray : .+}``.  Sixteen
    endpoints, seventeen handlers, seventeen distinct annotation values -- the last
    two differ because ``{z}/{x}/{y}.mvt`` is written in two classes.

    What the reviewer is told is *where* those strings are and whether they are
    collected into one place.
    """
    values: dict[str, list[str]] = {}
    for path, rel in srbscan.java_sources(repo):
        text = srbscan.read(path)
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in re.finditer(
                    r'@(?:[\w.]*\.)?(?:Path|RequestMapping|GetMapping|PostMapping'
                    r'|PutMapping|DeleteMapping|PatchMapping)\s*\(\s*'
                    r'(?:value\s*=\s*)?"([^"]*)"', line):
                values.setdefault(m.group(1), []).append(f"{rel}:{lineno}")

    if not values:
        pytest.fail(
            "REPORT: no path was found on a mapping annotation. If the submission "
            "routes without annotations -- a functional RouterFunction, a servlet "
            "mapping in web.xml, a hand-built HandlerMapping -- the paths are "
            "elsewhere and the reviewer has to find them.")

    pytest.fail(
        f"REPORT (not a defect): {len(values)} distinct annotation VALUE(s), which "
        f"is not the same number as the endpoints -- a class-level value combines "
        f"with a method-level one, and two classes here declare the same sub-path. "
        f"State A: 17 distinct values, which compose into 16 distinct endpoint "
        f"paths carrying 17 handlers.\n"
        + "\n".join(f"  {v!r}: {', '.join(sorted(set(where))[:3])}"
                    for v, where in sorted(values.items())[:28])
    )


# --------------------------------------------------------------------------
# checks: the shapes a hand-rolled dispatcher has
# --------------------------------------------------------------------------

def test_the_mappings_are_not_concentrated_in_one_class(repo):
    """A single class holding most of the handlers is the flattening tell.

    Not a defect on its own -- a small Spring application can put every
    ``@GetMapping`` on one ``@RestController`` and be idiomatic.  It is a defect on
    THIS repository, whose fourteen resources live in three Maven modules and whose
    ``NavigateResource`` is in ``navigation`` precisely so that module can be built
    without ``web``.  Collapsing them into one class does not just look different,
    it undoes a module boundary the reactor still declares.

    The threshold is 60% of method-level declarations in one file, or 10 in one
    file, whichever is reached first.  State A's maximum is 2 (``RouteResource``
    and ``NavigateResource``, each a GET and a POST).
    """
    per_class: dict[str, int] = {}
    for rel, uses in srbscan.mappings(repo).items():
        n = sum(len(lines) for name, lines in uses.items()
                if name in METHOD_ANNOTATIONS)
        if n:
            per_class[rel] = n
    total = sum(per_class.values())
    if total < 4:
        pytest.skip(f"only {total} method-level handler declaration(s) found; "
                    f"concentration is not a meaningful ratio below four")

    worst, count = max(per_class.items(), key=lambda kv: kv[1])
    share = count / total
    assert not (share >= 0.60 or count >= 10), (
        f"{worst} carries {count} of the tree's {total} method-level handler "
        f"declarations ({share:.0%}). State A spreads 17 over 14 classes with a "
        f"maximum of 2. One class holding this many is idiomatic Spring in a small "
        f"application and is also what flattening the resources into a dispatcher "
        f"looks like; read the class."
    )


#: The two things a hand-rolled router does that a container-dispatched
#: application has no reason to do: look at the request path itself, and decide
#: with a ``switch`` or an ``if``-chain.
_PATH_READS = ("getRequestURI(", "getPathInfo(", "getServletPath(",
               "getRequestURL(", "request.getPath(", "exchange.getRequest()")


@pytest.mark.parametrize("token", _PATH_READS)
def test_no_delivered_source_reads_the_raw_request_path(repo, token):
    """Reading the URI is how a dispatcher finds out what to dispatch.

    An annotated handler is handed its parameters; it does not need the raw path.

    State A reads one of these exactly once in ``src/main``, at
    ``navigation/.../NavigateResource.java:253``, and its own Javadoc explains why:
    the Mapbox Directions URL carries its coordinates as a semicolon-separated
    segment that no ``@PathParam`` can decode, so the resource parses the URI by
    hand.  That is a legitimate hit and it will appear on any faithful port, which
    is the calibration to keep in mind -- one hit in the navigation resource is
    State A's baseline.  Several hits, or a hit in a class that also carries the
    mappings, is the shape worth reading.
    """
    hits: dict[str, list[str]] = {}
    for path, rel in srbscan.java_sources(repo):
        if token in srbscan.read(path):
            hits[rel] = srbscan.cite(path, rel, token, limit=3)

    # The calibration goes in the MESSAGE, not only in the docstring above.  A
    # reviewer sees this check as a 300-character summary over a detail collapsed to
    # 240; the docstring reaches nobody.  Without it, the one hit that every
    # faithful port inherits from State A reads as a dispatcher smoking gun, and
    # `dispatch` is consulted by a required gate.
    baseline = (token == "getRequestURI("
                and list(hits) == ["navigation/src/main/java/com/graphhopper/"
                                   "navigation/NavigateResource.java"])
    if baseline:
        lead = (f"{token!r} appears once, in the navigation resource -- which is "
                f"State A's own baseline, not a finding on its own: the Mapbox "
                f"Directions URL carries its coordinates as a semicolon-separated "
                f"segment that no @PathParam can decode, so State A parses the URI "
                f"by hand here too. Worth reading only if this class also carries "
                f"the path mappings")
    else:
        expected = ("one hit, in navigation/../NavigateResource.java"
                    if token == "getRequestURI(" else "no hits at all")
        lead = (f"{token!r} appears in {len(hits)} delivered source file(s); an "
                f"annotated handler does not need the raw path, a dispatcher does. "
                f"State A's baseline for this token is {expected}")
    assert not hits, (
        lead + ":\n" + "\n".join(line for rel in sorted(hits) for line in hits[rel])
    )


def test_no_switch_or_chain_decides_on_the_request_path(repo):
    """The dispatcher's body, in the two forms Java writes it.

    A ``switch`` whose subject is a path-ish expression, or three or more
    ``equals``/``startsWith`` comparisons against path-shaped literals inside one
    method-sized window.  State A has neither and is silent here.

    The literal has to start with a slash, and that narrowing is deliberate.  State
    A's ``PtRedirectFilter`` compares ``getUriInfo().getPath()`` against ``"route"``
    and ``"isochrone"`` -- no slash, because Jersey hands the path relative to the
    application root -- and a check that also accepted bare words fired all over
    ``core``, where short string comparisons are how encoded values and custom
    models are parsed.  The consequence is honest and worth stating: a Spring port
    that compares against ``"route"`` the same way is not reported here either.
    What is reported is a dispatcher written against absolute paths, which is how
    one gets written when the servlet API is the input.

    The window is 40 lines rather than a real method boundary -- this is not a Java
    parser.  A class with a long ``if``-chain of legacy URL rewrites will trip it,
    which is why the citation lists the lines.
    """
    findings: list[str] = []
    path_ish = re.compile(r'\b(?:uri|url|path|route|endpoint|target|requestPath|'
                          r'pathInfo|servletPath)\b', re.I)
    compare = re.compile(r'\.(?:equals|equalsIgnoreCase|startsWith|matches)\s*\(\s*"(/[^"]*)"')

    for path, rel in srbscan.java_sources(repo):
        lines = srbscan.read(path).splitlines()
        for lineno, line in enumerate(lines, 1):
            if srbscan._COMMENT_LINE.match(line):
                continue
            m = re.search(r'\bswitch\s*\(([^)]*)\)', line)
            if m and path_ish.search(m.group(1)):
                findings.append(f"  {rel}:{lineno}: switch on "
                                f"{m.group(1).strip()[:60]!r}")
        # The if-chain form, counted in a sliding window.
        marks = [(i + 1, m.group(1))
                 for i, line in enumerate(lines)
                 if not srbscan._COMMENT_LINE.match(line)
                 for m in [compare.search(line.split("//", 1)[0])] if m]
        for idx, (lineno, _lit) in enumerate(marks):
            window = [mk for mk in marks[idx:] if mk[0] - lineno <= 40]
            if len(window) >= 3:
                findings.append(
                    f"  {rel}:{lineno}: {len(window)} path-literal comparison(s) "
                    f"within 40 lines: "
                    f"{', '.join(repr(l) for _, l in window[:5])}")
                break

    assert not findings, (
        f"{len(findings)} site(s) decide on the request path in code rather than "
        f"leaving it to a handler mapping. This is what a hand-written dispatcher "
        f"looks like; it is also what a redirect filter and a legacy-URL "
        f"compatibility shim look like. Read the sites:\n" + "\n".join(findings[:16])
    )


def test_no_servlet_or_filter_is_mounted_on_everything(repo):
    """One servlet on ``/*`` is a framework's worth of dispatch in one line.

    Looks for a ``HttpServlet`` subclass, a ``ServletRegistrationBean``, or a
    mapping literal of ``/*`` / ``/**`` attached to a servlet or filter
    registration.  State A subclasses no servlet; it has one ``Filter``
    (``CORSFilter``, registered by the bundle for header work) and Jersey's own
    servlet is installed by Dropwizard, not by this repository.

    A Spring port legitimately registers filters, and ``/**`` on a
    ``WebMvcConfigurer`` resource handler is how static assets get served -- that
    is why the check reports the line rather than the fact.
    """
    findings: list[str] = []
    for path, rel in srbscan.java_sources(repo):
        text = srbscan.read(path)
        if "extends HttpServlet" in text:
            findings.append(f"  {rel}:{srbscan.locate(path, 'extends HttpServlet')}: "
                            f"subclasses HttpServlet")
        for lineno, line in enumerate(text.splitlines(), 1):
            if srbscan._COMMENT_LINE.match(line):
                continue
            code = line.split("//", 1)[0]
            if "ServletRegistrationBean" in code:
                findings.append(f"  {rel}:{lineno}: {code.strip()[:120]}")
            elif re.search(r'"/\*\*?"', code) and re.search(
                    r'Servlet|Filter|addMapping|urlPatterns|DispatcherServlet',
                    code):
                findings.append(f"  {rel}:{lineno}: {code.strip()[:120]}")
    assert not findings, (
        f"{len(findings)} site(s) mount a servlet or filter across the whole path "
        f"space. A servlet on /* dispatches everything itself, whatever framework "
        f"is hosting it; a filter on /** is ordinary. Read the sites:\n"
        + "\n".join(findings[:16])
    )


def test_no_reflection_dispatches_on_an_annotation(repo):
    """Reflection plus a self-declared annotation is a framework, written here.

    ``getAnnotation`` / ``isAnnotationPresent`` on a class the tree also annotates,
    followed by ``Method.invoke``, is precisely how a JAX-RS-shaped dispatcher gets
    rebuilt without importing JAX-RS.  Both halves have to be present in one file
    for this to fire: State A uses reflection 5 times in ``src/main`` -- all of it
    in ``core`` for encoded-value and custom-model plumbing -- and never invokes a
    method it found by annotation.
    """
    findings: list[str] = []
    reads = ("getAnnotation(", "getDeclaredAnnotation(", "isAnnotationPresent(",
             "getAnnotations(")
    invokes = (".invoke(", "MethodHandle", "Method[] ", "getDeclaredMethods(")
    for path, rel in srbscan.java_sources(repo):
        text = srbscan.read(path)
        got_read = [t for t in reads if t in text]
        got_invoke = [t for t in invokes if t in text]
        if got_read and got_invoke:
            findings.append(
                f"  {rel}: reads annotations ({', '.join(got_read)}) and invokes "
                f"reflectively ({', '.join(got_invoke)})"
                + "\n" + "\n".join(srbscan.cite(path, rel, got_read[0], limit=2)))
    assert not findings, (
        f"{len(findings)} file(s) both read annotations reflectively and invoke "
        f"reflectively -- the two halves of a hand-written annotation dispatcher. "
        f"Read them:\n" + "\n".join(findings[:10])
    )


def test_the_resource_classes_are_still_where_the_reactor_puts_them(repo, original):
    """A handler that moved modules moved across a build boundary.

    Differential and coarse: for each Maven module that held handler-declaring
    annotations in State A, does the submission still declare handlers somewhere
    under that module?  ``navigation`` holding ``NavigateResource`` is the case
    that matters -- the reactor builds ``navigation`` before ``web`` and the
    behavioural stage's ``own_tests`` module runs ``navigation``'s tests as part of
    the whole-tree ``mvn test``, so a resource that migrated into ``web`` leaves
    those tests measuring nothing.

    Renaming files inside a module does not trip this, and neither does adding a
    module.  Only a module that had handlers and now has none.
    """
    def modules_with_handlers(tree):
        out: dict[str, int] = {}
        for rel, uses in srbscan.mappings(tree).items():
            n = sum(len(lines) for name, lines in uses.items()
                    if name in METHOD_ANNOTATIONS)
            if n:
                out[rel.split("/", 1)[0]] = out.get(rel.split("/", 1)[0], 0) + n
        return out

    before, after = modules_with_handlers(original), modules_with_handlers(repo)
    emptied = [f"  {mod}: {n} handler declaration(s) in State A, none now"
               for mod, n in sorted(before.items()) if mod not in after]
    assert not emptied, (
        f"{len(emptied)} Maven module(s) that declared handlers in State A declare "
        f"none in the submission; the reactor still builds them, and the "
        f"repository's own tests for them still run:\n" + "\n".join(emptied)
    )
