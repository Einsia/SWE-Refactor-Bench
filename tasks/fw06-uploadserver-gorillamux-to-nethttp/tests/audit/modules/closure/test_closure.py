"""Is the retired router gone, and was it replaced by nothing rather than by a peer?

Every check in this file is advisory.  None of them can fail the task on its own:
they are read into the reviewer's prompt as ``{{findings}}`` and the reviewer
decides what, if anything, they mean.  That division matters more here than
anywhere else in the ladder, because "no gorilla/mux left" is not a mechanical
property.  The whole retired surface is thirteen lines of registration and five
identifiers, so "the tree contains none of these strings" is satisfied by a `sed`
pass -- and the list of strings that would be checked doubles as the list of
identifiers to rename.  A submission can satisfy every assertion below and still
be running mux's dispatch loop under new type names in an internal package.
Conversely a submission can trip several of these and be a clean port: a comment
that says `// upstream matched this with PathPrefix, which is a string prefix`
names the retired API in order to explain the code that replaced it.

So the checks are ordered by how much of the answer they can carry.  The import
graph and the module graph come first, because those two are what the Go compiler
and the Go resolver read -- a submission that passes both cannot be linking mux.
The token sweep comes last and is the weakest: it reads text the compiler may
never look at, and a hit is a pointer for the reviewer to follow.

This task retires a *category*, not one library, so two ban lists are in play.
``retired-modules.txt`` holds the one module State A actually used and gets the
detailed checks; ``banned-routers.txt`` holds 28 module prefixes and gets the
coarse ones, because "mux was swapped for chi" is a different failure from "mux
is still here" and the reviewer should see which it is looking at.
"""

from __future__ import annotations

import re

import pytest

import srbscan

pytestmark = pytest.mark.scan

RETIRED = srbscan.retired_modules()

#: The banned category minus the module State A used.  The retired module is
#: excluded here because the checks below it get -- import graph, go.mod, go.sum,
#: vendoring -- say more about it than a prefix match can, and reporting the same
#: fact twice under two ids trains the reviewer to skim.
BANNED_OTHERS = [b for b in srbscan.banned_routers() if b not in RETIRED]


# --------------------------------------------------------------------------
# what the compiler reads
# --------------------------------------------------------------------------

@pytest.mark.parametrize("module", RETIRED)
def test_no_delivered_source_imports_a_retired_module(repo, module):
    """The strongest single observation in this file.

    ``srbscan.imports`` parses the import block rather than grepping the file, so
    this counts the module paths the compiler resolves and nothing else -- not a
    mention in a comment, not a URL in a doc string, not a line in a vendored
    copy.  A non-test source file that imports this is a source file that still
    links it.

    On this repository the expected count is exactly one before the work and zero
    after: mux is imported from ``pkg/server.go`` and from nowhere else.
    """
    hits = srbscan.importers(repo, module, tests=False)
    assert not hits, (
        f"{len(hits)} delivered source file(s) still import {module}:\n"
        + "\n".join(f"  {rel}: {', '.join(hits[rel])}" for rel in sorted(hits))
    )


@pytest.mark.parametrize("module", RETIRED)
def test_no_test_file_imports_a_retired_module(repo, module):
    """Split from the check above because the two mean different things.

    A ported handler that still imports mux is a port that did not happen.  A
    ported *test* that still imports mux is a test that was left behind -- and it
    is a leftover no later stage will report, because the behavioural stage builds
    against a mirror that CAN serve the retired router (it has to, to be able to
    compile the original) and stage 3 builds the submission against a pruned one
    only after this gate has already passed.  So this is the place it gets seen.

    Worth knowing about this repository specifically: **no** ``_test.go`` file in
    State A imports mux.  The unit suite drives handlers through
    ``httptest.NewRecorder`` and the e2e suite talks to a real socket, so the port
    does not force a test rewrite.  A test file that imports mux *after* the
    migration is therefore not a leftover -- it is new.
    """
    every = srbscan.importers(repo, module, tests=True)
    delivered = srbscan.importers(repo, module, tests=False)
    only_tests = {rel: lines for rel, lines in every.items() if rel not in delivered}
    assert not only_tests, (
        f"{len(only_tests)} test file(s) import {module}, which no test in the "
        "original did:\n"
        + "\n".join(f"  {rel}: {', '.join(only_tests[rel])}" for rel in sorted(only_tests))
    )


@pytest.mark.parametrize("module", RETIRED)
def test_go_mod_does_not_require_a_retired_module(repo, module):
    """A require line is not proof of linking, but it is proof of intent.

    Go keeps indirect requirements in ``go.mod``, so a retired module can survive
    here as somebody else's transitive dependency -- which is why this is advisory
    and why the message says which kind it is.  A *direct* require with nothing
    importing it usually means ``go mod tidy`` was never run, and that in turn
    means the delivered ``go.mod`` is not the one the submission builds from.

    Nothing else in this tree depends on mux, so after a correct port `go mod
    tidy` drops this line without being asked -- offline, from the ``.mod`` the
    delivery mirror deliberately keeps.
    """
    required = srbscan.required_modules(repo)
    if module not in required:
        return
    text = srbscan.gomod(repo)
    line = next(
        (ln.strip() for ln in text.splitlines() if ln.strip().startswith(module + " ")),
        module,
    )
    kind = "indirect" if "// indirect" in line else "DIRECT"
    importers = srbscan.importers(repo, module, tests=True)
    pytest.fail(
        f"go.mod still requires {module} ({kind}): {line}\n"
        f"  files importing it: {len(importers)}"
    )


@pytest.mark.parametrize("module", RETIRED)
def test_go_sum_does_not_pin_a_retired_module(repo, module):
    """Weaker than ``go.mod``, and reported anyway.

    ``go.sum`` accumulates hashes for everything the resolver has ever
    considered, so a leftover line here is normal after a partial cleanup and
    means little on its own.  It earns its place through the *reverse* reading: if
    go.sum still pins mux and go.mod no longer requires it, the tree was edited by
    hand rather than by the toolchain.
    """
    sums = repo / "go.sum"
    if not sums.is_file():
        pytest.fail("no go.sum in the delivered tree")
    lines = [
        ln.strip()
        for ln in srbscan.read(sums).splitlines()
        if ln.strip().startswith(module + " ")
    ]
    assert not lines, f"go.sum still pins {module} ({len(lines)} line(s)):\n" + "\n".join(
        f"  {ln}" for ln in lines[:4]
    )


@pytest.mark.parametrize("module", RETIRED)
def test_no_retired_module_is_vendored(repo, module):
    """``vendor/`` is exempt from every token scan, which is why this exists.

    Nothing else in this file reads inside a vendored copy -- and it should not,
    because the contents of a vendored dependency are upstream's code rather than
    the submission's.  The question here is only whether one is present.  A
    vendored mux would let the submission build against a mirror that no longer
    carries mux's zip, which is the one way to defeat the delivery mirror without
    touching a single import line.
    """
    vendored = srbscan.vendored_modules(repo)
    hits = [v for v in vendored if v == module or v.startswith(module + "/")]
    assert not hits, f"{module} is vendored in the delivered tree: {hits}"


# --------------------------------------------------------------------------
# the category, not the library
# --------------------------------------------------------------------------

@pytest.mark.parametrize("module", BANNED_OTHERS)
def test_no_banned_router_is_imported(repo, module):
    """Did a third-party router replace the third-party router?

    This is the check that makes fw06 a different task from a like-for-like
    router swap.  The destination is ``net/http.ServeMux``: since Go 1.22 the
    standard library has method patterns and wildcards, so the routing this
    program does needs no external library at all.  Replacing mux with chi
    satisfies every check above and is not this migration.

    Matched as a module-path prefix, so ``github.com/go-chi/chi`` also covers
    ``github.com/go-chi/chi/v5``.  Tests are included: a suite that imports a
    router is a suite that expects one to be there.
    """
    hits = srbscan.importers(repo, module, tests=True)
    assert not hits, (
        f"{module} is a third-party router and is imported by "
        f"{len(hits)} file(s):\n"
        + "\n".join(f"  {rel}: {', '.join(hits[rel])}" for rel in sorted(hits))
    )


@pytest.mark.parametrize("module", BANNED_OTHERS)
def test_no_banned_router_is_declared(repo, module):
    """The same question asked of the manifest rather than the import graph.

    Separate from the import check because the two fail in different orders.  A
    submission that ran ``go get github.com/go-chi/chi/v5`` and then abandoned the
    idea leaves a require line and no import; a submission that pasted an import
    without resolving it leaves an import and no require line and does not build.
    Both are worth seeing, and neither is what the reviewer should be told about
    the other.

    ``go.sum`` is read as well as ``go.mod``: the delivery mirror stocks none of
    these modules, so a sum line for one means the resolution happened somewhere
    the mirror was not.
    """
    required = [m for m in srbscan.required_modules(repo)
                if m == module or m.startswith(module + "/")]
    sums = sorted({
        ln.split()[0]
        for ln in srbscan.read(repo / "go.sum").splitlines()
        if ln.strip() and (ln.split()[0] == module
                           or ln.split()[0].startswith(module + "/"))
    })
    problems = []
    if required:
        problems.append(f"go.mod requires {required}")
    if sums:
        problems.append(f"go.sum pins {sums}")
    assert not problems, f"{module} is a banned third-party router: " + "; ".join(problems)


def test_no_replace_directive_relocates_a_module(repo):
    """The cleanest way to keep mux while appearing not to.

    ``replace github.com/gorilla/mux => ./internal/muxfork`` leaves every import
    path intact, satisfies the mirror without a network fetch, and reads as a
    one-line diff.  Any replace directive at all is worth the reviewer's attention
    on a task whose subject is a dependency change, so this reports the whole list
    rather than filtering to the retired name.
    """
    directives = srbscan.replace_directives(repo)
    assert not directives, (
        f"go.mod carries {len(directives)} replace directive(s):\n"
        + "\n".join(f"  {d}" for d in directives)
    )


def test_the_declared_dependency_set_actually_changed(repo, original):
    """A sanity check on the whole exercise.

    If the submission's require set is byte-identical to the original's then
    whatever else happened, no dependency was retired -- and every other check in
    this file is measuring an unmodified tree.  Reported early among the derived
    observations because it changes how the reviewer should read the rest.
    """
    before = set(srbscan.required_modules(original))
    after = set(srbscan.required_modules(repo))
    assert before != after, (
        f"go.mod requires the same {len(after)} modules as the original "
        f"({', '.join(sorted(after))}); nothing was retired and nothing was added"
    )


def test_the_module_path_is_unchanged(repo, original):
    """Renaming the module renames every import path in the tree.

    Not forbidden, but it invalidates the comparison the rest of the ladder
    performs -- stage 3 builds both trees and expects the same program to come
    out of each -- so the reviewer should know before reading anything else.
    """
    def path_of(tree):
        for line in srbscan.gomod(tree).splitlines():
            if line.strip().startswith("module "):
                return line.split(None, 1)[1].strip()
        return None

    assert path_of(repo) == path_of(original), (
        f"module path changed: {path_of(original)!r} -> {path_of(repo)!r}"
    )


def test_every_go_file_has_a_parseable_import_block(repo):
    """The blind spot in this module, measured rather than assumed.

    ``srbscan.imports`` reads a Go import block; it does not run the Go parser,
    because the stage-1 image has no Go toolchain.  A file it cannot read is a
    file the import checks above silently skipped, so an import could in principle
    hide there.  Flagging it is cheap and the count is normally zero.
    """
    unreadable = []
    for path, rel in srbscan.go_files(repo):
        text = srbscan.read(path)
        if not text.strip():
            continue
        if not re.search(r"^\s*package\s+\w+", text, re.M):
            unreadable.append(f"{rel}: no package clause")
            continue
        if re.search(r"^\s*import\s", text, re.M) and not srbscan.imports(path):
            unreadable.append(f"{rel}: an import statement no parser here could read")
    assert not unreadable, (
        f"{len(unreadable)} Go file(s) the import scan could not read:\n"
        + "\n".join(f"  {u}" for u in unreadable[:10])
    )


# --------------------------------------------------------------------------
# the shape of a router that was renamed instead of retired
# --------------------------------------------------------------------------

# mux's own API surface, spelled as it declares it: the methods on ``*Router`` and
# ``*Route``, plus the three package-level functions.  A hand-written router
# offering most of this under the same names has not replaced mux; it has renamed
# it.
#
# Counted per *file* rather than per type, and that detail is the check.  The
# obvious way to write this is to tally per declared type, which is what the
# equivalent check on a Gin task does -- Gin's whole surface hangs off one
# ``*gin.Context``, so a copy of it shows up as one type with a dozen matching
# methods.  mux does not work that way: it splits registration across ``*Router``
# (PathPrefix, Path, HandleFunc, Use, NewRoute) and chaining across ``*Route``
# (Methods, HandlerFunc, Handler, Subrouter).  Measured against a synthetic
# submission that copied mux into an internal package under a new name, the
# per-type tally scored 4 and 4 against a threshold of 5 and said nothing at all.
# Per file, the same tree scores 9.
_MUX_ROUTER_SURFACE = (
    "PathPrefix", "Path", "Methods", "Subrouter", "HandleFunc", "HandlerFunc",
    "Handler", "Use", "NewRoute", "Queries", "Headers", "GetPathTemplate",
    "GetPathRegexp", "Match", "Name", "SkipClean", "StrictSlash",
    "UseEncodedPath",
)

#: The package-level functions, which are not methods and so are missed by the
#: declaration regex below.  ``Vars`` is the one every mux handler calls.
_MUX_PACKAGE_FUNCS = ("Vars", "SetURLVars", "CurrentRoute", "NewRouter")

_METHOD_DECL = re.compile(r"^func\s*\(\s*\w+\s+\*?(\w+)\s*\)\s*(\w+)\s*\(", re.M)
_FUNC_DECL = re.compile(r"^func\s+(\w+)\s*\(", re.M)


def test_no_delivered_file_reimplements_the_retired_router(repo):
    """The one check here that looks for a *structure* rather than a name.

    It counts, per file, how much of mux's API the submission declares under mux's
    own spellings.  Two or three is nothing -- ``Handler``, ``Path`` and ``Match``
    are names anybody would reach for.  Most of the set in one file means the
    submission rebuilt mux's fluent registration API, which is a rewrite of the
    library rather than a migration off it, and it is the one cheat on this task
    that leaves the import graph and both manifests perfectly clean.

    The threshold is a judgement call, so the finding reports the full tally, split
    by the type each name was declared on, and leaves the judgement to the
    reviewer.
    """
    tally: dict[str, dict[str, set[str]]] = {}
    for path, rel in srbscan.go_sources(repo):
        text = srbscan.read(path)
        for typename, method in _METHOD_DECL.findall(text):
            if method in _MUX_ROUTER_SURFACE:
                tally.setdefault(rel, {}).setdefault(typename, set()).add(method)
        for func in _FUNC_DECL.findall(text):
            if func in _MUX_PACKAGE_FUNCS:
                tally.setdefault(rel, {}).setdefault("(package-level)", set()).add(func)

    def total(types):
        return len({m for ms in types.values() for m in ms})

    suspects = {rel: types for rel, types in tally.items() if total(types) >= 5}
    assert not suspects, "\n".join(
        f"{rel} declares {total(types)} of the retired router's API under its own "
        "spellings: "
        + "; ".join(f"{t} -> {sorted(ms)}" for t, ms in sorted(types.items()))
        for rel, types in sorted(suspects.items())
    )


def test_no_fluent_registration_chain_survives(repo):
    """mux's registration reads as a chain; ServeMux's cannot.

    ``r.PathPrefix("/files").Methods(...).HandlerFunc(...)`` is three calls on one
    line returning each other.  ``mux.Handle(pattern, handler)`` returns nothing,
    so a chained registration in the delivered tree means something other than
    ServeMux is being registered on -- a shim with mux's shape, most likely.

    Counted as "a call whose result has a method called on it, on a line that also
    registers a handler", which is narrow enough that the original scores five and
    a correct port scores zero.
    """
    chain = re.compile(r"\)\s*\.\s*(?:Methods|HandlerFunc|Handler|Subrouter|Queries|Headers|Name)\s*\(")
    hits: list[str] = []
    for path, rel in srbscan.go_sources(repo):
        for i, line in enumerate(srbscan.read(path).splitlines(), 1):
            if line.strip().startswith("//"):
                continue
            if chain.search(line):
                hits.append(f"{rel}:{i}: {line.strip()[:130]}")
    assert not hits, (
        f"{len(hits)} line(s) register a handler through a fluent chain, which "
        "net/http.ServeMux does not offer:\n" + "\n".join(f"  {h}" for h in hits[:10])
    )


# --------------------------------------------------------------------------
# the token sweep -- the weakest checks, deliberately last
# --------------------------------------------------------------------------

# Spellings only the retired library uses.  Each is a hit the reviewer can go and
# read; ``cite`` supplies the line so it does not have to open the file.
#
# Kept short on purpose.  Two spellings that look like candidates and are not:
#
#   * `NotFoundHandler` and `MethodNotAllowedHandler` are mux's field names, and
#     they are also the two most natural names for the handlers a port has to
#     write by hand -- ServeMux has no such fields, so a correct submission
#     supplies both behaviours itself and may well spell them this way.  A check
#     that fires on the honest answer is worse than no check.
#   * a bare `router` token matches State A's own `r := mux.NewRouter()` and its
#     README, and would match any port that named its variable `router`.
#
# What is left are the package-qualified calls and the two method names that
# exist nowhere in the standard library.
_RETIRED_TOKENS = (
    "mux.NewRouter", "mux.Router", "mux.Vars", "mux.SetURLVars",
    "mux.CurrentRoute", "mux.MiddlewareFunc", "mux.RouteMatch",
    "gorilla/mux", ".PathPrefix(", ".Subrouter(", ".Methods(http.Method",
)


@pytest.mark.parametrize("token", _RETIRED_TOKENS)
def test_no_retired_spelling_in_the_delivered_tree(repo, token):
    """A text search, with all the weakness that implies.

    This reads every non-exempt text file, so it sees comments, YAML, shell and
    configuration as well as code -- and a hit in any of those is not a
    dependency.  It is here because the alternative is missing the case where mux
    survives somewhere the import scan does not look: a generated file, a script
    that pins a version, a workflow that installs it.

    The most likely honest hit is a comment explaining what the old code did.
    That is a lead worth one file-open and nothing more.
    """
    hits: dict[str, list[str]] = {}
    for path, rel in srbscan.source_files(repo):
        if token in srbscan.read(path):
            hits[rel] = srbscan.cite(path, rel, token, limit=3)
    assert not hits, f"{token!r} appears in {len(hits)} file(s):\n" + "\n".join(
        line for rel in sorted(hits) for line in hits[rel]
    )


def test_the_build_automation_does_not_name_a_retired_module(repo):
    """Separated from the sweep above because the files are different.

    The Dockerfile, the compose file and the CI workflows are not compiled, so mux
    named in one of them cannot be a live import -- but it can be a ``go get``
    that reinstalls the module, or a pinned version the mirror is expected to
    serve.  Those are worth seeing on their own rather than buried in a token
    report.
    """
    roots = ["Makefile", "Dockerfile", "docker-compose.e2e.yml", "scripts",
             ".github", ".golangci.yml"]
    hits: list[str] = []
    for module in RETIRED:
        short = module.rsplit("/", 1)[-1]
        for name in roots:
            base = repo / name
            if not base.exists():
                continue
            paths = [base] if base.is_file() else [
                p for p in base.rglob("*") if p.is_file()
            ]
            for path in paths:
                rel = str(path.relative_to(repo))
                if srbscan.is_exempt(rel):
                    continue
                if module in srbscan.read(path):
                    hits.extend(srbscan.cite(path, rel, module, limit=2))
    assert not hits, "the build automation still names a retired module:\n" + "\n".join(
        hits[:12]
    )

