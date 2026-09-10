"""Has the retired stack left the tree, and left what the tree declares?

Advisory.  Nothing in this file can fail the audit gate: the gate is the six
prose questions in evaluation.toml, and this module's output reaches the reviewer
as findings to open rather than as verdicts to inherit.

The clearest case for the move is the word `express` itself.  In a delivered .js
file it is a real lead -- the shortcut this task exists to rule out is three lines
registering @fastify/express around the untouched application.  It is also what
you write in the comment that says *the Express middleware chain became four
hooks*, in the CHANGELOG entry describing the migration, and in a variable called
`expressStyleQuery` that has nothing to do with the framework.  A regex scoring
the token punishes the last three exactly as hard as the first.  Handing the line
number to a reader who can open the file costs them nothing.

The manifest and the lockfile are different, and this module treats them
differently: those are JSON, they are parsed, and a retired distribution declared
or locked there is a fact rather than a lead.  It is still reported as evidence,
because the reviewer is the one who decides what a fact about package.json means
for the gate -- but there is nothing approximate about the observation.

The exemptions instruction.md publishes are honoured: prose, and the two static
roots the service serves.  State A's own public/index.html is a json-server admin
page that mentions Express; a correct migration still ships it.
"""

from __future__ import annotations

import json
import re

import pytest
import srbscan

pytestmark = pytest.mark.scan

#: The bridges, called out separately from the rest of the retired list.  A
#: migration that needs one of these has translated its interface and kept its
#: implementation, so the reviewer is pointed at them first.
BRIDGES = ["@fastify/express", "@fastify/middie", "middie", "connect", "router",
           "express-promise-router"]

#: The subset of BRIDGES whose name cannot also be ordinary vocabulary, and so
#: can be searched for as free text rather than only as a specifier.
#:
#: `connect` and `router` are not on this list and that is not an oversight.
#: json-server's own public API is `jsonServer.router(db)`, its tests hold `let
#: router`, and "before the first connection" is a sentence in a comment about
#: listen().  Searching a tree for those two words produces a page of findings in
#: a correct migration, and a reviewer that has to dismiss a page of noise reads
#: the next page less carefully.  Both are still covered where the evidence is
#: unambiguous: as a specifier in the retired-name check above, and as a
#: registration shape in the lifecycle module.
UNAMBIGUOUS_BRIDGES = ["@fastify/express", "@fastify/middie",
                       "express-promise-router"]

#: Text that only appears in a copied Express / connect / body-parser source
#: tree.  A hit is a lead: the same strings appear in a migration note.
VENDORED_MARKERS = [
    "var app = function(req, res, next)",
    "exports = module.exports = createApplication",
    "app.request = Object.create(req",
    "res.__proto__ = app.response",
    "Layer.prototype.handle_request",
    "proto.process_params",
    "restore(done, obj)",
    "function trim_prefix",
    "This middleware is not compatible",
    "connect()",
    "createServer.prototype",
]

#: Express's own request/response surface.  Reported as a lead for the same
#: reason: `res.locals` in a comment describing what was removed reads exactly
#: like `res.locals` in a surviving handler.
EXPRESS_SURFACE = [
    "res.sendStatus(", "res.jsonp(", "res.locals", "req.originalUrl",
    "res.status(", "res.set(", "res.header(", "req.app", "res.app",
    "app.set(", "app.enable(", "app.disable(", "express.static",
    "express.Router", "express.json", "express.urlencoded", "app.route(",
    "res.sendFile(", "req.route", "next('route')",
]

#: Where a dependency can be declared, other than package.json.
MANIFEST_FILES = ["package.json", "package-lock.json", "npm-shrinkwrap.json",
                  "Dockerfile", "Procfile", ".npmrc", "serve.sh"]


# ---------------------------------------------------------------------------
# What the manifest declares.  Parsed, not matched.
# ---------------------------------------------------------------------------

def test_manifest_parses(repo):
    assert srbscan.load_json(repo / "package.json") is not None, (
        "package.json is missing or is not valid JSON; every observation below "
        "that reads it is therefore blank rather than clean"
    )


def test_manifest_declares_no_retired_distribution(repo, retired):
    declared = srbscan.declared_dependencies(repo)
    offenders = {name: spec for name, spec in declared.items()
                 if name in set(retired)}
    assert not offenders, (
        f"package.json declares retired distributions: "
        f"{json.dumps(offenders, indent=2)}"
    )


@pytest.mark.parametrize("bridge", BRIDGES)
def test_manifest_declares_no_bridge(bridge, repo):
    declared = srbscan.declared_dependencies(repo)
    assert bridge not in declared, (
        f"package.json declares {bridge}, an Express-to-Fastify bridge: "
        f"{bridge}@{declared.get(bridge)}"
    )


def test_manifest_declares_fastify(repo):
    declared = srbscan.declared_dependencies(repo)
    assert "fastify" in declared, (
        "package.json does not declare fastify; declared names are "
        f"{sorted(declared)}"
    )


def test_lockfile_locks_no_retired_distribution(repo, retired):
    locked = srbscan.locked_names(repo)
    offenders = sorted(locked & set(retired))
    assert not offenders, (
        f"a shipped lockfile resolves retired distributions: {offenders}"
    )


def test_no_bundled_dependencies(repo):
    """`bundleDependencies` ships a copy of a package inside the tarball."""
    manifest = srbscan.load_json(repo / "package.json") or {}
    bundled = (manifest.get("bundleDependencies")
               or manifest.get("bundledDependencies") or [])
    assert not bundled, (
        f"package.json bundles dependencies into the published artifact: "
        f"{bundled}"
    )


# ---------------------------------------------------------------------------
# What the sources name.  Text, and reported as text.
# ---------------------------------------------------------------------------

def test_no_source_specifier_names_a_retired_distribution(repo, retired):
    names = set(retired)
    offenders = []
    for path, rel in srbscan.code_files(repo):
        if srbscan.in_tests(rel):
            continue
        for lineno, spec, line in srbscan.specifier_lines(path):
            if srbscan.package_of(spec) in names:
                offenders.append({
                    "path": rel, "line": lineno, "specifier": spec,
                    "quote": line.strip()[:160],
                    "looks_commented": srbscan.looks_commented(line),
                })
    assert not offenders, (
        "these lines look like a module specifier naming a retired "
        f"distribution{srbscan.comment_caveat(offenders)}:\n"
        f"{json.dumps(offenders[:40], indent=2)}"
    )


def test_no_test_specifier_names_a_retired_distribution(repo, retired):
    """The project's own suite, reported separately.

    A ported suite that still requires `supertest` is a finding about the suite
    rather than about the service, and the gate asks about both -- so it is a
    separate observation with its own line numbers.
    """
    names = set(retired)
    offenders = []
    for path, rel in srbscan.code_files(repo):
        if not srbscan.in_tests(rel):
            continue
        for lineno, spec, line in srbscan.specifier_lines(path):
            if srbscan.package_of(spec) in names:
                offenders.append({"path": rel, "line": lineno,
                                  "specifier": spec,
                                  "quote": line.strip()[:160],
                                  "looks_commented":
                                      srbscan.looks_commented(line)})
    assert not offenders, (
        "the project's own tests name retired distributions"
        f"{srbscan.comment_caveat(offenders)}:\n"
        f"{json.dumps(offenders[:40], indent=2)}"
    )


@pytest.mark.parametrize("bridge", UNAMBIGUOUS_BRIDGES)
def test_no_source_line_mentions_a_bridge(bridge, repo):
    offenders = []
    for path, rel in srbscan.source_files(repo):
        if path.name in ("package-lock.json", "npm-shrinkwrap.json"):
            continue
        for lineno, line in srbscan.lines(path):
            if bridge in line:
                offenders.append({"path": rel, "line": lineno,
                                  "quote": line.strip()[:160],
                                  "looks_commented":
                                      srbscan.looks_commented(line)})
    assert not offenders, (
        f"these lines mention {bridge}{srbscan.comment_caveat(offenders)}:\n"
        f"{json.dumps(offenders[:20], indent=2)}"
    )


def test_no_dynamic_require_of_a_retired_distribution(repo, retired):
    """A specifier assembled at run time, which the plain scan above misses."""
    pattern = re.compile(
        r"""(?:require|import)\s*\(\s*(?:[A-Za-z_$][\w$]*|`[^`]*`|"""
        r"""['"][^'"]*['"]\s*\+)""")
    names = set(retired)
    offenders = []
    for path, rel in srbscan.code_files(repo):
        text = srbscan.read(path)
        if not pattern.search(text):
            continue
        for lineno, line in srbscan.lines(path):
            if not pattern.search(line):
                continue
            if any(n in line for n in names):
                offenders.append({"path": rel, "line": lineno,
                                  "quote": line.strip()[:160],
                                  "looks_commented":
                                      srbscan.looks_commented(line)})
    assert not offenders, (
        "these lines build a module specifier at run time and name a retired "
        f"distribution on the same line{srbscan.comment_caveat(offenders)}:\n"
        f"{json.dumps(offenders[:20], indent=2)}"
    )


@pytest.mark.parametrize("marker", VENDORED_MARKERS)
def test_no_vendored_copy_of_the_retired_stack(marker, repo):
    """Text from a copied Express source tree, cited with the line.

    ``VENDORED_MARKERS`` says of itself that "a hit is a lead: the same strings
    appear in a migration note", and two entries make that concrete -- ``connect()``
    and ``This middleware is not compatible`` are ordinary English about the thing
    being migrated away from, so the likeliest place either occurs in a correct
    submission is a comment or a docstring explaining what was removed.

    So the citation carries the line's text and the comment flag, like the sibling
    checks in this module.  ``code_files`` is JavaScript-only, hence
    ``looks_commented``'s default.
    """
    offenders = []
    for path, rel in srbscan.code_files(repo):
        entry = srbscan.marker_citation(path, rel, marker)
        if entry:
            offenders.append(entry)
    assert not offenders, (
        f"{marker!r} appears in the tree; it is text that occurs in a copied "
        f"Express or connect source file{srbscan.comment_caveat(offenders)}: "
        f"{json.dumps(offenders[:10], indent=2)}"
    )


@pytest.mark.parametrize("symbol", EXPRESS_SURFACE)
def test_express_api_surface_is_not_called(symbol, repo):
    offenders = []
    for path, rel in srbscan.code_files(repo):
        if srbscan.in_tests(rel):
            continue
        for lineno, line in srbscan.lines(path):
            if symbol in line:
                offenders.append({"path": rel, "line": lineno,
                                  "quote": line.strip()[:160],
                                  "looks_commented":
                                      srbscan.looks_commented(line)})
    assert not offenders, (
        f"{symbol!r} is Express's API surface and appears here"
        f"{srbscan.comment_caveat(offenders)}:\n"
        f"{json.dumps(offenders[:20], indent=2)}"
    )


# ---------------------------------------------------------------------------
# Where else a dependency can hide
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", MANIFEST_FILES)
def test_no_retired_name_in_a_manifest_file(name, repo, retired):
    path = repo / name
    if not path.is_file():
        pytest.skip(f"{name} is not in the tree")
    if name in ("package-lock.json", "npm-shrinkwrap.json"):
        offenders = sorted(srbscan.locked_names(repo) & set(retired))
        assert not offenders, f"{name} resolves {offenders}"
        return
    hits = []
    for lineno, line in srbscan.lines(path):
        for dist in retired:
            if re.search(rf"(?<![\w@/-]){re.escape(dist)}(?![\w-])", line):
                # Four of these files comment with `#`, not `//` -- Dockerfile,
                # Procfile, .npmrc and serve.sh -- so the flag is given the path
                # and answers in the right language.  package.json has no comment
                # syntax at all, where the JavaScript default correctly says no.
                hits.append({"line": lineno, "name": dist,
                             "quote": line.strip()[:160],
                             "looks_commented":
                                 srbscan.looks_commented(line, path)})
    assert not hits, (
        f"{name} names retired distributions{srbscan.comment_caveat(hits)}: "
        f"{json.dumps(hits[:20], indent=2)}"
    )


def test_no_installed_tree_is_shipped(repo):
    """A committed `node_modules/` is how a retired package arrives unnoticed."""
    installed = repo / "node_modules"
    if not installed.is_dir():
        return
    present = sorted(p.name for p in installed.iterdir() if p.is_dir())
    raise AssertionError(
        f"node_modules/ is present in the submitted tree with {len(present)} "
        f"entries: {present[:30]}"
    )


def test_no_vendor_directory(repo):
    suspicious = []
    for path in sorted(repo.rglob("*")):
        if not path.is_dir():
            continue
        rel = str(path.relative_to(repo))
        if srbscan.is_exempt(rel):
            continue
        if path.name in ("vendor", "vendored", "third_party", "thirdparty",
                         "external", "deps", "_vendor"):
            suspicious.append(rel)
    assert not suspicious, (
        f"these directory names are where a copied dependency usually lands: "
        f"{suspicious}"
    )


def test_no_tarball_or_archive_in_the_tree(repo):
    archives = []
    for path in sorted(repo.rglob("*")):
        if path.is_file() and path.suffix in (".tgz", ".tar", ".gz", ".zip"):
            archives.append(str(path.relative_to(repo)))
    assert not archives, (
        f"these archives are in the tree; a file: or tarball dependency is one "
        f"way a retired package survives: {archives[:20]}"
    )


def test_no_file_or_git_dependency(repo):
    """`file:`, `link:`, a git URL or a tarball URL sidesteps the registry."""
    declared = srbscan.declared_dependencies(repo)
    offenders = {name: spec for name, spec in declared.items()
                 if re.match(r"^(file:|link:|git|https?:|github:|portal:)", spec)}
    assert not offenders, (
        f"these dependencies do not resolve through the registry: "
        f"{json.dumps(offenders, indent=2)}"
    )


def test_no_npm_override_or_alias(repo):
    """An alias installs one package under another's name."""
    manifest = srbscan.load_json(repo / "package.json") or {}
    findings = {}
    for table in ("overrides", "resolutions"):
        if manifest.get(table):
            findings[table] = manifest[table]
    aliases = {name: spec for name, spec
               in srbscan.declared_dependencies(repo).items()
               if spec.startswith("npm:")}
    if aliases:
        findings["aliases"] = aliases
    assert not findings, (
        f"package.json rewrites what a name resolves to: "
        f"{json.dumps(findings, indent=2)}"
    )


# ---------------------------------------------------------------------------
# What State A named, for the reviewer's orientation
# ---------------------------------------------------------------------------

def test_original_is_mounted(original):
    assert (original / "package.json").is_file(), (
        f"{original} does not look like State A; the comparisons below are "
        "therefore blank rather than clean"
    )


def test_every_original_runtime_dependency_is_accounted_for(repo, original,
                                                            retired):
    """Dropped runtime names that are neither retired nor still declared.

    Not a defect on its own -- `morgan` should be gone and is retired, and a
    migration may legitimately drop `pluralize` if it inlined the two calls it
    made.  It is the list the reviewer wants when deciding whether a capability
    left with its dependency.

    Runtime only.  State A's devDependencies are a babel toolchain and a jest
    suite, and a migration that drops the babel build to publish `src/` directly
    drops all four `@babel/*` packages as a consequence -- correct, expected, and
    twelve lines of noise if this check read every table.
    """
    before = set(srbscan.runtime_dependencies(original))
    after = set(srbscan.declared_dependencies(repo))
    unexplained = sorted(before - after - set(retired))
    assert not unexplained, (
        "State A declared these as runtime dependencies and State B declares "
        f"neither them nor a retirement for them: {unexplained}"
    )
