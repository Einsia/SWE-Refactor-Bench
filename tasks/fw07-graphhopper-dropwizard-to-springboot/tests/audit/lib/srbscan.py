"""Fixtures and helpers for the read-only scan.

Deliberately small.  A scan module gets two trees and a text editor's worth of
facility: open a file, walk a directory, read the import block of a Java file,
parse a pom.  It cannot resolve, compile, package or run either tree, and there is
nothing here that would let it — the stage-1 image has no JDK and no Maven, so a
module that tried would fail on the missing toolchain rather than quietly grading a
build.

WHAT THIS TASK MAKES HARD, AND WHY THE HELPERS LOOK LIKE THIS.  The retired stack
is not a library that gets called; it is a set of annotations and a registration
call.  `@Path`/`@GET`/`@Produces` on a class, an `AbstractBinder` wiring it,
`environment.jersey().register(...)` at the bootstrap.  A submission can define
annotations with exactly those names in its own package, write a servlet that
reflects over them, and satisfy every token check while the dispatch is still
JAX-RS-shaped and none of Spring's is doing anything.

So the helpers here are built to hand a reviewer the two facts that distinguish
those cases, and to refuse to draw the conclusion themselves:

  ``imports`` reads the import BLOCK, not the file.  A regex over the whole file
  counts `jakarta.ws.rs` in a comment, in a CHANGELOG sentence and in a Javadoc
  link.  The import block is what the compiler reads.

  ``annotation_uses`` reports where an annotation is APPLIED, and
  ``annotation_declarations`` reports where one is DEFINED.  Those two being the
  same repository is the whole hand-rolled-framework case, and no single number
  expresses it — a reviewer needs to see that `@Path` is applied in twelve files
  and declared in this one.

  ``mappings`` collects both stacks' request-mapping annotations into one list
  without ranking them, because "how many distinct handlers are registered" is the
  question the `spring_does_the_dispatching` gate turns on, and a submission with
  one mapping and a `switch` inside it is the shape that count reveals.

Nothing here returns a verdict.  Every check in this stage is recorded with
required = false and `grade_audit` gates on required checks only, so a finding
costs a submission nothing by itself; what it does is put a path and a line number
in front of a reviewer that has the file open.  A clean scan is not a pass, and
prompt.txt says so in those words.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO = Path(os.environ.get("SRB_REPO", "/opt/workspace"))
ORIGINAL = Path(os.environ.get("SRB_ORIGINAL", "/opt/original"))

_DATA = Path(__file__).resolve().parent.parent / "data"

#: The retired stack, read from the same file the environment image prunes with
#: and the behavioural stage reports on.  One list in the tree: a stack added to
#: the ban is covered here without editing a scan module.
RETIRED_FILE = Path(os.environ.get("SRB_RETIRED_STACK",
                                   str(_DATA / "retired-stack.txt")))

#: Maven's namespace.  Every pom in this repository declares it, and ElementTree
#: reports tags fully qualified, so every lookup has to go through it.
_POM_NS = {"m": "http://maven.apache.org/POM/4.0.0"}

#: Prose and binary assets.  A migration is documented in Markdown, and this
#: repository ships map tiles, OSM extracts, fonts and a bundled UI; none of them
#: is a place to look for a live dependency.
EXEMPT_SUFFIXES = (".md", ".rst", ".txt", ".png", ".jpg", ".jpeg", ".webp",
                   ".svg", ".ico", ".gz", ".tgz", ".zip", ".jar", ".bin",
                   ".pbf", ".osm", ".ghz", ".mvt", ".pem", ".key", ".woff",
                   ".woff2", ".ttf", ".map", ".class")

#: Directories a scan does not read.  ``target`` is build output — if a submission
#: shipped one, what is in it says nothing about the source that was written.
EXEMPT_DIRS = (".git", "target", "node_modules", "__pycache__", ".cache",
               ".mvn", "dist", "build")

#: Suffixes a text scan will open at all.  The empty string catches Dockerfile,
#: LICENSE, Makefile and the extensionless fixtures.
TEXT_SUFFIXES = (".java", ".xml", ".yml", ".yaml", ".json", ".properties",
                 ".toml", ".sh", ".bash", ".cfg", ".ini", ".sql", ".html",
                 ".css", ".js", ".ts", ".factories", ".imports", "")


def _list_file(path: Path, kind: str) -> list[str]:
    """The values of one `kind` column in retired-stack.txt, in declared order.

    The file carries two kinds of name — ``group`` (a Maven groupId prefix) and
    ``package`` (a Java package prefix) — because the artifacts and the imports do
    not spell the same thing: Dropwizard's metrics library is published under
    ``io.dropwizard.metrics`` and its classes live under ``com.codahale.metrics``.
    A scan that conflated them would look for a package that no pom declares and
    an artifact no source imports.
    """
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) == 2 and parts[0] == kind:
            out.append(parts[1].strip())
    return out


def retired_groups() -> list[str]:
    """Maven groupId prefixes the task retires."""
    return _list_file(RETIRED_FILE, "group")


def retired_packages() -> list[str]:
    """Java package prefixes the task retires."""
    return _list_file(RETIRED_FILE, "package")


def is_exempt(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in EXEMPT_DIRS for p in parts):
        return True
    return rel.endswith(EXEMPT_SUFFIXES)


def read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def source_files(repo: Path):
    """Every text file worth reading, as ``(path, repo-relative string)``."""
    for path in sorted(repo.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(repo))
        if is_exempt(rel):
            continue
        if path.suffix in TEXT_SUFFIXES:
            yield path, rel


def java_files(repo: Path):
    """Every ``.java`` file, tests included."""
    for path, rel in source_files(repo):
        if path.suffix == ".java":
            yield path, rel


def _is_test(rel: str) -> bool:
    """Whether a path is test source, by Maven's own convention.

    ``src/test/java`` rather than a filename pattern: Maven decides what is test
    scope by directory, and a submission is free to name a test class anything.
    """
    return "src/test/" in rel.replace("\\", "/")


def java_sources(repo: Path):
    """Every ``.java`` file under ``src/main``.

    The split matters for almost every check here.  A test is allowed to know it
    is a test, and a test that still constructs the retired framework is a
    different finding from a handler that does.
    """
    for path, rel in java_files(repo):
        if not _is_test(rel):
            yield path, rel


def java_tests(repo: Path):
    for path, rel in java_files(repo):
        if _is_test(rel):
            yield path, rel


_IMPORT_LINE = re.compile(r'^\s*import\s+(?:static\s+)?([\w.]+(?:\.\*)?)\s*;')


def imports(path: Path) -> set[str]:
    """The types a Java file imports, read from its import statements.

    Line-anchored rather than a regex over the file, for the reason in the module
    docstring: a package name in a comment or a Javadoc `{@link}` is not an
    import.  Static imports are included and the `static` keyword dropped, since
    ``import static io.dropwizard.X.y`` is a dependency on ``io.dropwizard`` by any
    reading.  A trailing ``.*`` is kept as written so a citation shows the
    wildcard.

    Not a Java parser.  It does not know about block comments, so an import
    commented out with `/* */` spanning lines is still reported — which is why
    nothing in this stage is scored, and why every finding carries the line for a
    reviewer to open.
    """
    found: set[str] = set()
    for line in read(path).splitlines():
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        m = _IMPORT_LINE.match(line)
        if m:
            found.add(m.group(1))
    return found


def importers(repo: Path, prefix: str, *, tests: bool = True) -> dict[str, list[str]]:
    """Files whose import block names ``prefix`` or a type inside it.

    Keyed by repo-relative path, valued by the imports found, so a finding names
    the type rather than only the package.
    """
    walker = java_files if tests else java_sources
    out: dict[str, list[str]] = {}
    for path, rel in walker(repo):
        hit = sorted(p for p in imports(path)
                     if p == prefix or p.startswith(prefix + "."))
        if hit:
            out[rel] = hit
    return out


# --------------------------------------------------------------------------
# poms
# --------------------------------------------------------------------------

def poms(repo: Path) -> list[tuple[Path, str]]:
    """Every ``pom.xml``, as ``(path, repo-relative string)``."""
    return [(p, str(p.relative_to(repo)))
            for p in sorted(repo.rglob("pom.xml"))
            if not is_exempt(str(p.relative_to(repo)))]


def _text(node, tag: str) -> str:
    found = node.find(f"m:{tag}", _POM_NS)
    return (found.text or "").strip() if found is not None else ""


def pom_dependencies(path: Path) -> list[dict]:
    """Every ``<dependency>`` in a pom, wherever it is declared.

    Includes `dependencyManagement` and the `<dependencies>` of a plugin, because
    all three can put the retired framework back on a classpath and a reviewer
    asking "is it still declared" means any of them.  Each entry carries `where`
    so the citation can say which.

    Returns dicts rather than tuples: `scope` and `type` decide what a declaration
    means — a `test`-scoped dependency on the retired framework is a stale test,
    and `<type>pom</type>` with `<scope>import</scope>` is a BOM, which is how this
    repository pulls in the whole stack in one line.
    """
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return []
    out: list[dict] = []
    for dep in root.iter("{http://maven.apache.org/POM/4.0.0}dependency"):
        group = _text(dep, "groupId")
        artifact = _text(dep, "artifactId")
        if not group and not artifact:
            continue
        # Where the declaration sits, by walking up: managed, plugin-scoped, or a
        # direct dependency of the module.
        out.append({
            "group": group,
            "artifact": artifact,
            "version": _text(dep, "version"),
            "scope": _text(dep, "scope"),
            "type": _text(dep, "type"),
            "coord": f"{group}:{artifact}",
        })
    return out


def pom_managed_imports(path: Path) -> list[dict]:
    """The BOMs a pom imports — ``<type>pom</type>`` with ``<scope>import</scope>``.

    Its own check because a BOM is how a whole stack arrives in one declaration:
    State A's root pom imports ``io.dropwizard:dropwizard-dependencies``, and a
    submission that removed every direct dependency but kept that line has kept
    the versions of the entire retired stack available to any module that asks.
    """
    return [d for d in pom_dependencies(path)
            if d["type"] == "pom" and d["scope"] == "import"]


def pom_modules(path: Path) -> list[str]:
    """The ``<module>`` names a pom declares, in declared order.

    A reactor that lost a module is a repository that no longer builds part of
    itself, and the behavioural stage would then compare a jar that is missing code
    rather than a jar that answers differently.
    """
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return []
    return [(m.text or "").strip()
            for m in root.iter("{http://maven.apache.org/POM/4.0.0}module")
            if (m.text or "").strip()]


def pom_plugins(path: Path) -> list[str]:
    """``group:artifact`` for every plugin a pom declares or manages."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return []
    out = []
    for pl in root.iter("{http://maven.apache.org/POM/4.0.0}plugin"):
        group = _text(pl, "groupId") or "org.apache.maven.plugins"
        artifact = _text(pl, "artifactId")
        if artifact:
            out.append(f"{group}:{artifact}")
    return out


def declared_coords(repo: Path) -> dict[str, list[str]]:
    """``group:artifact`` -> the poms that declare it, across the whole tree."""
    out: dict[str, list[str]] = {}
    for path, rel in poms(repo):
        for dep in pom_dependencies(path):
            out.setdefault(dep["coord"], []).append(rel)
    return out


# --------------------------------------------------------------------------
# citations
# --------------------------------------------------------------------------

def cite(path: Path, rel: str, token: str, limit: int = 6) -> list[str]:
    """Every line ``token`` appears on, as ``rel:lineno: text`` citations.

    Whole lines, because a reviewer is going to re-open the file at that line and
    a bare line number does not say what to expect there.
    """
    out = []
    for lineno, line in enumerate(read(path).splitlines(), 1):
        if token in line:
            out.append(f"{rel}:{lineno}: {line.strip()[:140]}")
            if len(out) >= limit:
                out.append(f"{rel}: (further hits not listed)")
                break
    return out


def locate(path: Path, needle: str) -> int | None:
    """The 1-based line ``needle`` first appears on, for a citation."""
    for lineno, line in enumerate(read(path).splitlines(), 1):
        if needle in line:
            return lineno
    return None


# --------------------------------------------------------------------------
# task-specific readers
# --------------------------------------------------------------------------

#: An annotation being applied.  Captures the last segment, so a fully-qualified
#: use reports the same name as a short one: this repository writes
#: ``@jakarta.ws.rs.Path("match")`` on MapMatchingResource and ``@Path`` on the
#: twelve others, and a reader that saw two different annotations there would
#: report the migration as one resource further along than it is.
_ANNOTATION = re.compile(r'@((?:\w+\.)*)(\w+)')

#: Lines a Javadoc or comment owns.  ``@author``, ``@param``, ``@return`` and
#: ``{@link}`` are annotation-shaped and are not annotations; this repository has
#: an ``@author`` tag on most classes, so without the skip every file reports one.
_COMMENT_LINE = re.compile(r'^\s*(?:\*|/\*|//)')


def annotation_uses(path: Path) -> dict[str, list[int]]:
    """Annotation simple-name -> the lines it is applied on.

    Comment lines are skipped, and so is anything after a ``//`` on a code line.
    It is not a parser: an annotation inside a string literal counts, and one
    inside a ``/* */`` block that does not start its line is not skipped.  Those
    are the errors a reviewer catches by opening the cited line, which is why the
    citation is always emitted with the count.
    """
    out: dict[str, list[int]] = {}
    for lineno, line in enumerate(read(path).splitlines(), 1):
        if _COMMENT_LINE.match(line):
            continue
        code = line.split("//", 1)[0]
        for _qual, name in _ANNOTATION.findall(code):
            out.setdefault(name, []).append(lineno)
    return out


#: An annotation TYPE being declared.  ``public @interface Path`` is the whole
#: hand-rolled-framework move: the tokens a scan looks for all appear, and they
#: are the submission's own.
_ANNOTATION_DECL = re.compile(r'@interface\s+(\w+)')


def annotation_declarations(repo: Path) -> dict[str, list[str]]:
    """Annotation simple-name -> ``rel:lineno`` for every ``@interface`` declared.

    Reported next to ``annotation_uses`` rather than instead of it, because the
    two together are the finding and neither is alone.  State A declares none of
    the dispatch annotations it uses -- they arrive from ``jakarta.ws.rs`` -- so a
    submission that declares ``@Path``, ``@GetMapping`` or ``@RestController``
    itself has written the framework rather than adopted one, and the reviewer can
    read the class and say whether the container is dispatching through it.
    """
    out: dict[str, list[str]] = {}
    for path, rel in java_files(repo):
        for lineno, line in enumerate(read(path).splitlines(), 1):
            if _COMMENT_LINE.match(line):
                continue
            for name in _ANNOTATION_DECL.findall(line.split("//", 1)[0]):
                out.setdefault(name, []).append(f"{rel}:{lineno}")
    return out


#: The annotations that declare an HTTP handler on either stack.  Both lists are
#: collected into one place and neither is preferred, because the gate asks how
#: many handlers are registered and where -- not which vocabulary they use.
JAXRS_METHOD_ANNOTATIONS = ("GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS",
                            "PATCH")
SPRING_MAPPING_ANNOTATIONS = ("RequestMapping", "GetMapping", "PostMapping",
                              "PutMapping", "DeleteMapping", "PatchMapping")
SPRING_CLASS_ANNOTATIONS = ("RestController", "Controller", "Configuration",
                            "Component", "Service", "Repository",
                            "SpringBootApplication", "ControllerAdvice",
                            "RestControllerAdvice")


def mappings(repo: Path) -> dict[str, dict[str, list[int]]]:
    """Per source file, the handler-declaring annotations it applies.

    ``{rel: {annotation: [lineno, ...]}}`` over ``src/main`` only, restricted to
    the two vocabularies above plus ``@Path``.  A reviewer uses it two ways: how
    many handler declarations there are -- State A carries 17 method-level ones
    (14 ``@GET``, 3 ``@POST``) and 18 ``@Path`` across 14 classes -- and where they
    sit.  One class carrying every mapping is the shape a ``switch`` on the request
    path hides in, and it does not look different from a correct port in any single
    total.
    """
    watch = set(JAXRS_METHOD_ANNOTATIONS) | set(SPRING_MAPPING_ANNOTATIONS) | {
        "Path"} | set(SPRING_CLASS_ANNOTATIONS)
    out: dict[str, dict[str, list[int]]] = {}
    for path, rel in java_sources(repo):
        uses = {name: lines for name, lines in annotation_uses(path).items()
                if name in watch}
        if uses:
            out[rel] = uses
    return out


#: How a handler gets registered by hand.  Jersey's ``register`` and Spring's
#: ``addResourceHandlers``/``addViewControllers`` are both here: the question is
#: whether registration is enumerated in code, and a hand-rolled
#: ``HandlerMapping`` populated from a list is the same answer as
#: ``jersey().register`` was.
_REGISTRATION = (
    "jersey().register(",
    "ResourceConfig",
    "register(",
    "addServlet(",
    "addFilter(",
    "addMapping(",
    "registerBean(",
    "SimpleUrlHandlerMapping",
    "RouterFunction",
    "RouterFunctions.route(",
)
# A bare ``route(`` was tried here and removed: this is a routing engine, so
# ``hopper.route(req)`` is its most-called method and the sweep returned 40-odd
# lines from ``core`` and ``example`` that have nothing to do with HTTP.  The
# functional-endpoint form is spelled ``RouterFunctions.route(`` and that is what
# is listed.


def registrations(path: Path) -> list[tuple[int, str]]:
    """``(lineno, line)`` for every call that looks like registering a handler.

    Broad on purpose and reported without interpretation.  ``register(`` alone
    matches Jackson module registration and a metrics registry, so the list is
    noisy; what it is for is showing a reviewer the bootstrap file's shape in one
    place, and the bootstrap is where a migration either moved the wiring into the
    container or kept an enumerated list and renamed it.
    """
    out = []
    for lineno, line in enumerate(read(path).splitlines(), 1):
        if _COMMENT_LINE.match(line):
            continue
        code = line.split("//", 1)[0]
        if any(tok in code for tok in _REGISTRATION):
            out.append((lineno, line.strip()[:160]))
    return out


def extends_or_implements(path: Path) -> set[str]:
    """Simple names this file's type declarations extend or implement.

    Used for the two ends of the bootstrap: State A's entry point is
    ``extends Application<GraphHopperServerConfiguration>`` and its bundle is
    ``implements ConfiguredBundle``.  A submission that still declares either has
    kept the retired framework's contract even if no import survives, since a
    submission may have written its own ``Application`` class.
    """
    found: set[str] = set()
    text = read(path)
    for m in re.finditer(r'\b(?:extends|implements)\s+([\w.,<>\s]+?)\s*\{', text):
        for part in re.split(r'[,<>]', m.group(1)):
            name = part.strip().split(".")[-1]
            if name:
                found.add(name)
    return found


def yaml_and_properties(repo: Path):
    """Config files either stack reads, as ``(path, rel)``.

    Both frameworks are configured by file and the migration moves the file:
    Dropwizard reads a YAML handed to it on the command line, Spring Boot reads
    ``application.yml``/``application.properties`` off the classpath.  Which one a
    submission ships is a fact about the port, and the behavioural stage separately
    proves the CLI still accepts the original YAML -- so a submission may well
    have both, and the count alone is not a finding.
    """
    for path, rel in source_files(repo):
        if path.suffix in (".yml", ".yaml", ".properties"):
            yield path, rel


def spring_factories(repo: Path) -> list[str]:
    """Spring's registration files, if any: ``META-INF/spring*`` under resources.

    ``org.springframework.boot.autoconfigure.AutoConfiguration.imports`` and the
    legacy ``spring.factories`` register configuration without an annotation, so a
    reviewer looking for where beans come from has to be told they exist.
    """
    out = []
    for path, rel in source_files(repo):
        if "META-INF" in rel and (path.name.startswith("spring")
                                  or path.name.endswith(".imports")
                                  or path.name == "spring.factories"):
            out.append(rel)
    return out


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def repo() -> Path:
    _require_tree(REPO, "the submission", "SRB_REPO")
    return REPO


@pytest.fixture(scope="session")
def original() -> Path:
    _require_tree(ORIGINAL, "State A", "SRB_ORIGINAL")
    return ORIGINAL


def _require_tree(path: Path, role: str, var: str) -> None:
    """Refuse to scan against a tree that is not there.

    Every comparison in this stage is *differential*: it asks what the submission
    has that State A did not, or no longer has.  Against an empty `original` that
    question inverts into "what does the submission have", and the answers still
    look like findings -- a differential import check against nothing reports every
    import in a correctly ported tree as newly introduced, and a pom diff against
    nothing reports the whole reactor as added.  A reviewer reads those as
    evidence, because nothing in them says the comparison had one side missing.

    So the failure has to arrive here, once, naming the mount, rather than as a
    dozen plausible-looking defects downstream.  Both trees are mount points in
    this stage's image (empty by design); an empty one at *runtime* means the
    harness did not mount it, which is an infrastructure fault and never something
    the submission did.
    """
    if not path.is_dir() or not any(path.iterdir()):
        state = "does not exist" if not path.is_dir() else "is an empty directory"
        pytest.fail(
            f"INFRASTRUCTURE FAULT, not a finding about the submission: the tree "
            f"for {role} ({path}, from ${var}) {state}. This stage compares the "
            f"submission against State A, and every check it runs is differential; "
            f"with one side absent the comparisons still produce output, and that "
            f"output is meaningless. Mount {role} at {path} and re-run. Do not "
            f"read any finding from this run.")


@pytest.fixture(scope="session")
def groups() -> list[str]:
    return retired_groups()


@pytest.fixture(scope="session")
def packages() -> list[str]:
    return retired_packages()
