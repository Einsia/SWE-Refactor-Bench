"""Is the retired stack actually gone, or only hidden?

Every check in this file is advisory.  None of them can fail the task on its
own: they are read into the reviewer's prompt as ``{{findings}}`` and the
reviewer decides what, if anything, they mean.  That division matters here
more than anywhere else in the ladder, because "no Gin left" is not a
mechanical property.  A submission can satisfy every assertion below and
still be a Gin server -- copy the framework's dispatch loop into
``pkg/chartmuseum/router/`` under new type names and no token search will
ever see it.  Conversely a submission can trip several of these and be a
clean port: a ``_test.go`` file that still names ``gin.Context`` is a stale
test, not a live dependency, and a stray ``gin`` line in a CHANGELOG is
prose.

So the checks are ordered by how much of the answer they can actually
carry.  The import graph and the module graph come first, because those two
are what the Go compiler and the Go resolver read -- a submission that
passes both cannot be linking Gin.  The token sweep comes last and is the
weakest: it reads text the compiler may never look at, and a hit is a
pointer for the reviewer to follow rather than a verdict.
"""

from __future__ import annotations

import re

import pytest

import srbscan

pytestmark = pytest.mark.scan

RETIRED = srbscan.retired_modules()


# --------------------------------------------------------------------------
# what the compiler reads
# --------------------------------------------------------------------------

@pytest.mark.parametrize("module", RETIRED)
def test_no_delivered_source_imports_a_retired_module(repo, module):
    """The strongest single observation in this file.

    ``srbscan.imports`` parses the import block rather than grepping the
    file, so this counts the module paths the compiler resolves and nothing
    else -- not a mention in a comment, not a URL in a doc string, not a
    line in a vendored copy.  A non-test source file that imports one of
    these is a source file that still links it.
    """
    hits = srbscan.importers(repo, module, tests=False)
    assert not hits, (
        f"{len(hits)} delivered source file(s) still import {module}:\n"
        + "\n".join(f"  {rel}" for rel in sorted(hits))
    )


@pytest.mark.parametrize("module", RETIRED)
def test_no_test_file_imports_a_retired_module(repo, module):
    """Split from the check above because the two mean different things.

    A ported handler that still imports Gin is a port that did not happen.
    A ported *test* that still imports Gin is a test that was left behind --
    it may not even compile, which the behavioural stage will discover when
    it runs the repository's own suite.  Worth reporting separately so the
    reviewer can tell a dead test from a live dependency.
    """
    every = srbscan.importers(repo, module, tests=True)
    delivered = srbscan.importers(repo, module, tests=False)
    only_tests = {rel: lines for rel, lines in every.items() if rel not in delivered}
    assert not only_tests, (
        f"{len(only_tests)} test file(s) still import {module}:\n"
        + "\n".join(f"  {rel}" for rel in sorted(only_tests))
    )


@pytest.mark.parametrize("module", RETIRED)
def test_go_mod_does_not_require_a_retired_module(repo, module):
    """A require line is not proof of linking, but it is proof of intent.

    Go keeps indirect requirements in ``go.mod``, so a retired module can
    legitimately survive here as somebody else's transitive dependency --
    that is why this is advisory and why the message says which kind it is.
    A *direct* require with nothing importing it is the interesting case:
    it usually means ``go mod tidy`` was never run, which in turn means the
    delivered ``go.mod`` is not the one the submission builds from.
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
    """Weaker than ``go.mod`` and reported anyway.

    ``go.sum`` accumulates hashes for everything the resolver has ever
    considered, including modules no longer required, so a leftover line
    here is normal after a partial cleanup and means very little on its
    own.  It earns its place because of the *reverse* reading: if go.sum
    still pins Gin and go.mod does not require it, the tree was edited by
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

    Nothing else in this file reads inside a vendored copy -- and it should
    not, because the contents of a vendored dependency are upstream's code,
    not the submission's.  The question here is only whether one is present.
    A vendored Gin would let the submission build against a mirror that no
    longer carries Gin's zip, which is the one way to defeat the delivery
    mirror without touching a single import line.
    """
    vendored = srbscan.vendored_modules(repo)
    hits = [v for v in vendored if v == module or v.startswith(module + "/")]
    assert not hits, f"{module} is vendored in the delivered tree: {hits}"


def test_no_replace_directive_relocates_a_retired_module(repo):
    """The cleanest way to keep Gin while appearing not to.

    ``replace github.com/gin-gonic/gin => ./internal/ginfork`` leaves the
    import paths intact, satisfies the mirror without a network fetch, and
    reads as a one-line diff.  Any replace directive at all is worth the
    reviewer's attention on a task whose whole point is a dependency
    change, so this reports the full list rather than filtering to the
    three retired names.
    """
    directives = srbscan.replace_directives(repo)
    assert not directives, (
        f"go.mod carries {len(directives)} replace directive(s):\n"
        + "\n".join(f"  {d}" for d in directives)
    )


def test_the_declared_dependency_set_actually_changed(repo, original):
    """A sanity check on the whole exercise.

    If the submission's require set is byte-identical to the original's,
    then whatever else happened, no dependency was retired and none was
    added -- and every other check in this file is measuring an unmodified
    tree.  Reported first among the derived observations because it changes
    how the reviewer should read the rest.
    """
    before = set(srbscan.required_modules(original))
    after = set(srbscan.required_modules(repo))
    assert before != after, (
        f"go.mod requires the same {len(after)} modules as the original; "
        "nothing was retired and nothing was added"
    )


def test_the_module_path_is_unchanged(repo, original):
    """Renaming the module renames every import path in the tree.

    That is not forbidden, but it invalidates the comparison the rest of
    the ladder performs -- the behavioural stage builds both trees and
    expects the same binary name and the same package layout -- so the
    reviewer should know before reading anything else.
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

    ``srbscan.imports`` reads a Go import block; it does not run the Go
    parser, because the stage-1 image has no Go toolchain.  A file it
    cannot read is a file the two import checks above silently skipped, so
    a submission could in principle hide an import there.  Flagging it is
    cheap and the count is normally zero.
    """
    unreadable = []
    for path, rel in srbscan.go_files(repo):
        text = srbscan.read(path)
        if not text.strip():
            continue
        if not re.search(r"^\s*package\s+\w+", text, re.M):
            unreadable.append(f"{rel}: no package clause")
            continue
        if "import" in text and not srbscan.imports(path) and re.search(
            r'^\s*import\s', text, re.M
        ):
            unreadable.append(f"{rel}: an import statement no parser here could read")
    assert not unreadable, (
        f"{len(unreadable)} Go file(s) the import scan could not read:\n"
        + "\n".join(f"  {u}" for u in unreadable[:10])
    )


# --------------------------------------------------------------------------
# the shape of a framework that was renamed instead of retired
# --------------------------------------------------------------------------

# Gin's request context is the part of the framework a handler touches on
# every line, so a hand-rolled copy of it is the most likely thing to find
# in a submission that wanted to avoid rewriting the handlers.  These are
# the method names Gin's *gin.Context publishes that a net/http port has no
# reason to reinvent under the same spelling: net/http gives a handler an
# http.ResponseWriter and an *http.Request directly.
_GIN_CONTEXT_METHODS = (
    "AbortWithStatus", "AbortWithStatusJSON", "AbortWithError", "Abort",
    "JSON", "Next", "Param", "Set", "Get", "ShouldBind", "Data",
)
_METHOD_DECL = re.compile(r"^func\s*\(\s*\w+\s+\*?(\w+)\s*\)\s*(\w+)\s*\(", re.M)

# The dispatch half of a context type, as opposed to its accessors.  Gin's
# own is `handlers HandlersChain` plus `index int8`, walked by Next().
_CHAINY = re.compile(r"handler|middleware|chain", re.I)
_INDEX_NAMES = {"index", "idx", "i", "pos", "cursor", "step"}
_INT_TYPE = re.compile(r"^u?int(8|16|32|64)?$")


def _struct_body(text: str, typename: str) -> str | None:
    """The field list of ``type <typename> struct { ... }``, or None.

    Brace-counted rather than regex-matched, so an embedded anonymous struct
    cannot truncate the body.  None means no struct declaration for the name
    was found in this file -- which the caller reports as undetermined, never
    as an absence.

    Both spellings are matched: a top-level ``type X struct {`` and an
    indented ``X struct {`` inside a grouped ``type ( ... )`` block.  This
    codebase uses the grouped form, and anchoring on ``type`` alone would
    report every type in it as undeclared.
    """
    name = re.escape(typename)
    opener = re.search(r"^type\s+" + name + r"\s+struct\s*\{", text, re.M)
    if not opener:
        # grouped form; also matches an embedded anonymous field of the same
        # name, which is rare and still worth showing the reviewer.
        opener = re.search(r"^[ \t]+" + name + r"\s+struct\s*\{", text, re.M)
    if not opener:
        return None
    depth, i = 1, opener.end()
    while i < len(text) and depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[opener.end():i - 1]


def _struct_fields(body: str) -> list[tuple[str, str]]:
    """``(name, type)`` for each field line, comments and tags stripped."""
    fields: list[tuple[str, str]] = []
    for raw in body.splitlines():
        line = raw.split("//", 1)[0].split("`", 1)[0].strip()
        if not line or line.startswith("*") or line.endswith("{"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isidentifier():
            fields.append((parts[0], parts[1].strip()))
    return fields


def _is_chain(name: str, ftype: str) -> bool:
    """Could this field hold a handler chain for the type to walk?

    Deliberately generous -- it feeds a report the reviewer opens the file to
    check, so a field named for a slice of handlers is worth naming even if it
    turns out to be something else.  A single ``http.Handler`` is not a chain,
    which is why a slice or the word "chain" is required.
    """
    if re.search(r"chain", f"{name} {ftype}", re.I):
        return True
    if not ftype.startswith("[]"):
        return False
    return bool(_CHAINY.search(ftype) or _CHAINY.search(name) or "func" in ftype)


def test_no_delivered_type_reimplements_the_retired_context(repo):
    """The one check here that looks for a *structure* rather than a name.

    It counts, per declared type, how many of Gin's context methods the
    submission defines under the same spellings.  One or two is nothing --
    ``JSON`` and ``Data`` are ordinary names.  A high tally on a single type
    means the port supplied its own context object carrying most of what the
    old one carried.

    That on its own is *expected work*, not a finding: this codebase's handler
    bodies take a context, so a port to ``net/http`` has to give them one, and
    the task explicitly allows it ("your own carrier", "handlers keeping a
    familiar shape behind a thin adapter of your own is a style choice").  A
    correct port lands around seven of these names -- ``Set``/``Get`` for the
    value store, ``Param`` for route parameters, ``JSON``/``Data`` for writes,
    ``Abort``/``AbortWithStatus`` because the handlers call them.

    What separates that from the framework rewritten under a new name is the
    *dispatch* machinery, not the accessors: ``Next`` plus a handler slice and
    an index to walk it.  So the finding reports whether those are present, and
    the reviewer weighs the tally with that in hand.  A high tally without
    ``Next`` is the shape of an adapter; with ``Next`` it is the shape of Gin.
    """
    sources = [(rel, srbscan.read(path)) for path, rel in srbscan.go_sources(repo)]
    tally: dict[str, dict[str, set[str]]] = {}
    for rel, text in sources:
        for typename, method in _METHOD_DECL.findall(text):
            if method in _GIN_CONTEXT_METHODS:
                tally.setdefault(typename, {}).setdefault(rel, set()).add(method)

    def methods_of(files: dict[str, set[str]]) -> list[str]:
        return sorted({m for ms in files.values() for m in ms})

    suspects = {t: f for t, f in tally.items() if len(methods_of(f)) >= 5}
    if not suspects:
        return

    # The tally alone cannot separate an adapter from the framework rewritten
    # under new names, because the difference is in the dispatch fields and
    # those live on the struct rather than in the method set.
    shape: dict[str, dict] = {}
    for typename in suspects:
        shape[typename] = {"where": "?", "fields": None, "chain": [], "index": []}
        for rel, text in sources:
            body = _struct_body(text, typename)
            if body is None:
                continue
            fields = _struct_fields(body)
            shape[typename] = {
                "where": rel,
                "fields": len(fields),
                "chain": [n for n, ft in fields if _is_chain(n, ft)],
                "index": [n for n, ft in fields
                          if n.lower() in _INDEX_NAMES and _INT_TYPE.match(ft)],
            }
            break

    # Only the FIRST line of this message reaches the reviewer: scan.digest
    # renders `summary` -- which is this line -- and collapses the rest to a
    # single 240-char `detail`. So the conclusion goes on line 1, and the
    # per-type breakdown after it is for whoever opens the result file.
    # Three groups, not two.  The exculpating sentence may only be written when
    # BOTH halves of the dispatch machinery were looked for and neither was
    # found -- otherwise a type with Next() whose chain field this scan failed
    # to recognise, or whose struct declaration it could not locate at all,
    # would be described as "no Next(), no chain" and cleared on the strength
    # of the scan's own blind spot.  Ambiguity goes to the reviewer.
    def halves(t: str) -> tuple[bool, bool]:
        return "Next" in methods_of(suspects[t]), bool(shape[t]["chain"])

    rehosted = sorted(t for t in suspects if all(halves(t)))
    unclear = sorted(t for t in suspects
                     if any(halves(t)) and not all(halves(t))
                     or (shape[t]["fields"] is None and not any(halves(t))))
    clean = sorted(t for t in suspects if t not in rehosted and t not in unclear)

    def tag(t: str) -> str:
        n = len(methods_of(suspects[t]))
        return f"{t} {n}/{len(_GIN_CONTEXT_METHODS)} in {shape[t]['where']}"

    # The type names and their files lead, because this line is truncated at
    # 300 characters on the way into the prompt and the identity of the type is
    # the only part of it a reviewer can act on.  Prose goes after, where it can
    # be cut without costing anything.
    group = rehosted or unclear or clean
    verb = "carries" if len(group) == 1 else "carry"
    tags = "; ".join(tag(t) for t in group)

    if rehosted:
        head = (
            f"{tags}: {verb} Gin's context accessors AND its dispatch machinery "
            f"-- Next() plus a handler chain to walk. That is the framework "
            f"re-hosted under a new name, not an adapter over net/http; read it "
            f"against Gin's own context."
        )
    elif unclear:
        head = (
            f"{tags}: {verb} many of Gin's context method names and part of its "
            f"dispatch machinery -- Next() or a chain but not both, or a struct "
            f"this scan could not read. Adapter or framework re-hosted is yours "
            f"to settle."
        )
    else:
        head = (
            f"{tags}: {verb} many of Gin's context method names, but no Next() "
            f"and no handler chain to walk -- accessors only, the adapter shape "
            f"this task expects. The tally alone is not a finding."
        )

    lines = [head, ""]
    for t in rehosted + unclear + clean:
        s = shape[t]
        where = (f"{s['fields']} field(s) in {s['where']}"
                 if s["fields"] is not None
                 else "no struct declaration found -- fields undetermined")
        lines.append(
            f"  {t}: methods {', '.join(methods_of(suspects[t]))}\n"
            f"    Next(): {'PRESENT' if 'Next' in methods_of(suspects[t]) else 'absent'}"
            f"; handler chain: {', '.join(s['chain']) or 'none'}"
            f"; walk index: {', '.join(s['index']) or 'none'}\n"
            f"    {where}; methods declared in {', '.join(sorted(suspects[t]))}"
        )
    pytest.fail("\n".join(lines))


# --------------------------------------------------------------------------
# the token sweep -- the weakest checks, deliberately last
# --------------------------------------------------------------------------

# Spellings that only the retired stack uses.  Each one is a hit the
# reviewer can go and read for itself; ``cite`` supplies the line so it
# does not have to open the file.  Kept short on purpose: a long list of
# generic tokens produces noise, and noise is what makes a reviewer stop
# reading findings.
_RETIRED_TOKENS = (
    "gin.Context", "gin.HandlerFunc", "gin.Engine", "gin.New(", "gin.Default(",
    "gin.SetMode", "gin.ReleaseMode", "gin.DebugMode", "gin.Recovery",
    "gin.LoggerWithConfig", "gin.ErrorType", "gin.IRoutes", "gin.RouterGroup",
    "gin.H{", "ginprometheus", "RequestSizeLimiter",
)


@pytest.mark.parametrize("token", _RETIRED_TOKENS)
def test_no_retired_spelling_in_the_delivered_tree(repo, token):
    """A text search, with all the weakness that implies.

    This reads every non-exempt text file, so it sees comments, YAML,
    shell and documentation as well as code -- and a hit in any of those
    is not a dependency.  It is here because the alternative is missing
    the case where Gin survives somewhere the import scan does not look:
    a generated file, a script that pins a version, a workflow that
    installs it.

    Because of that the finding leads with the code/comment tally, and it
    has to: the reviewer reads this through ``scan.digest``, which cuts
    detail at 240 characters.  Measured on a real submission -- a plain
    net/http port with zero Gin in go.mod, go.sum, imports and vendor --
    the two hits were both ``//`` comments, one of them the agent saying
    it had replaced ``*gin.Context``, and the cut landed one character
    before the first ``//`` with the second file past the end.  So a tree
    that had fully retired Gin reached a required gate as "'gin.Context'
    appears in 2 file(s)".  A correct port provokes this more than a
    careless one, since documenting the change is what puts the retired
    spelling in the tree.
    """
    hits: dict[str, list[str]] = {}
    code = prose = 0
    for path, rel in srbscan.source_files(repo):
        if token in srbscan.read(path):
            cites, in_code, in_prose = srbscan.cite_classified(
                path, rel, token, limit=3)
            hits[rel] = cites
            code += in_code
            prose += in_prose
    where = (f"{code} in code, {prose} in comments or prose"
             if code else
             f"NOT IN CODE: all {prose} are comments or prose, which is not a "
             f"dependency -- read them before treating this as a survival")
    assert not hits, (
        f"{token!r} appears in {len(hits)} file(s), {where}:\n"
        + "\n".join(line for rel in sorted(hits) for line in hits[rel])
    )


def test_the_build_automation_does_not_name_a_retired_module(repo):
    """Separated from the sweep above because the files are different.

    The Makefile, the scripts and the CI workflows are not compiled, so a
    Gin reference in one of them cannot be a live import -- but it can be a
    ``go get`` that reinstalls the module, or a pinned version the mirror is
    expected to serve.  Those are worth seeing on their own rather than
    buried in a token report.
    """
    roots = ["Makefile", "Dockerfile", "scripts", ".github", "acceptance_tests"]
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
                text = srbscan.read(path)
                if module in text or f"/{short}" in text:
                    hits.extend(srbscan.cite(path, rel, short, limit=2))
    assert not hits, "the build automation still names a retired module:\n" + "\n".join(
        hits[:12]
    )
