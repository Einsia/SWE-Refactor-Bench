"""Where does a request get matched, and is the program that runs the one that was rewritten?

Two kinds of check live here and they read differently in the findings digest, so
it is worth saying which is which up front.

Most of this file behaves like the rest of stage 1: it asserts something and stays
silent unless the tree disagrees.  A handful of checks are **reports** -- they
have nothing to be silent about, because their whole output is a transcription of
what the submission registered and where.  Those are named ``report_*`` and their
message opens with ``REPORT``.  They are emitted through the same failure channel
as everything else because that is the only channel the digest renders: the
prompt shows failures in full and passes as a count, so a check that had something
to say and said it by passing would have said it into a void.

A report is not an accusation.  Reading one as a defect is the specific mistake
the prompt warns against, and the digest repeats the warning.  The reason they
exist is that ``dispatch_is_rehosted`` -- the gate this module feeds -- turns on a
distinction a sentence can state and a regex cannot:

    is ServeMux choosing between several registered patterns, with a fallback
    handling the residue -- or is there one pattern, with everything decided
    inside it?

"Eight patterns, six carrying a method" and "one pattern and a `switch r.Method`
inside it" are both spelled with the same identifiers.  Handing the reviewer the
pattern list with line numbers turns a twenty-minute grep into a thirty-second
read, and the reviewer still has to open the file to answer the gate.

The second half of the module is about what actually runs.  A correct rewrite that
is not wired to the entry point, or a container that still builds the old file, is
a failure the behavioural stage would report as a mystery -- the tree reads well and
the binary answers wrongly.  These are ordinary assertions and are silent on an
unmodified tree.
"""

from __future__ import annotations

import re

import pytest

import srbscan

pytestmark = pytest.mark.scan


def _sources(tree):
    return list(srbscan.go_sources(tree))


# --------------------------------------------------------------------------
# reports: what was registered, and where
# --------------------------------------------------------------------------

def test_report_the_registration_table(repo):
    """REPORT. Every routing registration in the delivered tree, as written.

    ServeMux construction, ``Handle``/``HandleFunc`` with their pattern literals,
    ``http.Handle`` on the package-level default mux, and the ``Handler`` field of
    an ``http.Server`` -- which mounts one handler for everything and is therefore
    the single most informative line in the file when it is the only entry.

    The counts at the end are the part the gate turns on: how many distinct
    patterns, and how many of them carry a Go 1.22 method prefix.  State A shows
    two ``HandleFunc`` calls on one pattern plus a ``Handler:`` field, because its
    other five registrations go through mux's fluent API and are not spelled this
    way at all.

    Registrations whose pattern is COMPUTED -- ``mux.Handle(method+" /", h)``, a
    pattern from a variable or a map key -- are counted separately and said out
    loud, because the counts cannot see them and silence about them reads as a
    fact.  Measured on a real submission: a loop registering six method patterns
    was reported as "1 distinct pattern literal, 0 carry a method prefix", which
    is the fronting shape's signature and the opposite of what the tree did.  An
    unreadable pattern is a reason to open the file, never a count of zero.

    That warning LEADS the message, ahead of the counts it corrects, and the
    ordering is the fix rather than a preference.  The reviewer does not read this
    text; it reads a digest that renders the first 240 characters of it, and ~83
    of those go to the pytest frame.  Measured: with the warning appended after
    the counts, the reviewer's copy ended ``... without: ['/'] E 1 furt`` -- the
    misleading counts arrived in full and the correction was cut off mid-word.  A
    caveat that loses a race with truncation is not a caveat.
    """
    table: list[str] = []
    patterns: dict[str, int] = {}
    computed = 0
    for path, rel in _sources(repo):
        for lineno, call, pattern in srbscan.registrations(path):
            # One classification, used for both the row's label and the counter.
            # They were separate, and drifted: every patternless row was labelled
            # READ THIS LINE while only some were counted as computed, so the
            # report pointed the reviewer at three lines and claimed all three
            # were hiding something when one was.  READ THIS LINE is a claim that
            # something is concealed on that line; spending it on a line with no
            # pattern to have spends the reviewer's attention on nothing.
            if pattern:
                patterns[pattern] = patterns.get(pattern, 0) + 1
                shown = pattern
            elif call.startswith("Handler"):
                # A `Handler:` field has no pattern by nature: it mounts one
                # handler for everything, and the next report covers it.
                shown = "(no pattern: mounts one handler for everything)"
            elif "NewServeMux" in call:
                shown = "(construction, not a registration)"
            else:
                computed += 1
                shown = "(pattern COMPUTED at runtime -- READ THIS LINE)"
            table.append(f"{rel}:{lineno}: {call} {shown}")

    if not table:
        pytest.fail(
            "REPORT: no routing registration of any recognised shape was found in "
            "the delivered non-test sources. Either dispatch is built somewhere "
            "this scan does not look, or the request path does not go through a mux "
            "at all."
        )

    with_method = sorted(p for p in patterns if srbscan.pattern_method(p))
    without = sorted(p for p in patterns if not srbscan.pattern_method(p))

    if computed:
        # Kept short on purpose: this whole sentence has to survive a 240-char
        # window that the pytest frame has already eaten 83 of.
        head = (f"REPORT (not a defect): {computed} line(s) build a pattern at "
                f"RUNTIME and may register many, so every count below is a LOWER "
                f"BOUND, not a fact.")
        counts = (f" Among the {len(patterns)} readable literal(s), "
                  f"{len(with_method)} carry a method prefix and {len(without)} "
                  f"do not; {len(table)} routing line(s) in total.")
    else:
        head = f"REPORT (not a defect): {len(table)} routing line(s)"
        counts = (f" over {len(patterns)} distinct pattern literal(s); "
                  f"{len(with_method)} carry a method prefix, {len(without)} "
                  f"do not.")
    pytest.fail(
        head + counts + "\n"
        f"  with a method: {with_method}\n"
        f"  without:       {without}\n"
        # Quantified so the reviewer can check the claim against the table rather
        # than take it on trust.
        + (f"  exactly {computed} line(s) below are marked READ THIS LINE; open "
           f"them and see what the pattern is built from\n" if computed else "")
        + "\n".join(f"  {t}" for t in table[:24])
    )


def test_report_where_the_server_mounts_its_handler(repo):
    """REPORT. The line that decides what the listening socket talks to.

    ``http.Server{Handler: x}``, ``srv.Handler = x``, ``http.ListenAndServe(addr,
    x)`` and ``http.Serve(l, x)`` are the four ways this program could mount its
    dispatcher.  Whatever ``x`` is, it is what every request meets first: a
    submission whose new ServeMux is built correctly and never reaches this line is
    a submission whose new ServeMux does not run.

    State A mounts mux's ``*Router`` here.  A correct port mounts a ``*ServeMux``,
    or a middleware chain that ends at one.
    """
    mount = re.compile(
        r"\bHandler\s*:\s*(\S+?),?\s*$|"
        r"\.Handler\s*=\s*(\S+)|"
        r"http\.ListenAndServe(?:TLS)?\s*\([^,]+,\s*([^)]+)\)|"
        r"http\.Serve\s*\([^,]+,\s*([^)]+)\)"
    )
    hits: list[str] = []
    for path, rel in _sources(repo):
        for lineno, line in enumerate(srbscan.read(path).splitlines(), 1):
            if line.strip().startswith("//"):
                continue
            if mount.search(line):
                hits.append(f"{rel}:{lineno}: {line.strip()[:130]}")
    if not hits:
        pytest.fail(
            "REPORT: no line in the delivered non-test sources mounts a handler "
            "onto a server. The listening socket's handler could not be located by "
            "reading, which is worth resolving before answering the dispatch gate."
        )
    pytest.fail(
        f"REPORT (not a defect): {len(hits)} line(s) mount a handler onto a "
        "server or a listener:\n" + "\n".join(f"  {h}" for h in hits[:12])
    )


def test_report_the_prefix_rule_as_implemented(repo):
    """REPORT. Every string-prefix comparison, with the paths in view.

    **A correct submission trips this by construction.**  State A matched
    ``/files`` with ``r.PathPrefix("/files")``, which is a match on the request
    path's *string* prefix and not on a path segment, so ``/filesabc`` matched the
    files routes and ``/file`` matched nothing.  ServeMux cannot express that:
    ``/files/`` is a subtree pattern on a segment boundary.  The rule therefore has
    to be written by hand, and this is where the hand-written version shows up.

    Its presence says nothing.  What the ``prefix_rule_is_derived`` gate asks is
    whether the code *decides* -- whether a path the corpus never contains gets an
    answer computed from a rule -- or whether the interesting paths are enumerated.
    Both look like a ``strings.HasPrefix`` from here.  Reported so the reviewer
    starts at the right lines; State A itself has three.
    """
    hits: list[str] = []
    for path, rel in _sources(repo):
        text = srbscan.read(path).splitlines()
        for lineno in srbscan.prefix_tests(path):
            hits.append(f"{rel}:{lineno}: {text[lineno - 1].strip()[:130]}")
    if not hits:
        pytest.fail(
            "REPORT: no string-prefix comparison anywhere in the delivered "
            "non-test sources. The original matched /files on a string prefix, "
            "which ServeMux's subtree patterns do not reproduce, so a port with no "
            "prefix test has either implemented the rule some other way or changed "
            "the behaviour."
        )
    pytest.fail(
        f"REPORT (not a defect, the original has three): {len(hits)} "
        "string-prefix comparison(s):\n" + "\n".join(f"  {h}" for h in hits[:14])
    )


def test_report_where_the_request_paths_are_named(repo):
    """REPORT. Request-path literals, per file, with counts.

    ``/upload`` and ``/files`` are this task's contract, so a submission with none
    of them would be the surprising one -- the literals are not a smell.  What the
    reviewer wants from this report is *concentration*: a handful of literals at
    the registration site is a router, while a dozen of them in one table beside
    status codes is a lookup, and the second is what
    ``answers_are_computed`` exists to catch.

    Reported per file with a total so the shape is visible without opening
    anything.  State A names five in ``pkg/server.go`` and none anywhere else.
    """
    per_file: list[tuple[str, int, list[str]]] = []
    total = 0
    for path, rel in _sources(repo):
        lits = srbscan.path_literals(path)
        if not lits:
            continue
        count = sum(len(v) for v in lits.values())
        total += count
        per_file.append((rel, count, sorted(lits)[:12]))
    if not per_file:
        pytest.fail(
            "REPORT: no request-path literal in the delivered non-test sources. "
            "This program's contract is written in terms of /upload and /files, so "
            "a tree with neither is worth a look."
        )
    per_file.sort(key=lambda row: -row[1])
    pytest.fail(
        f"REPORT (not a defect): {total} request-path literal occurrence(s) in "
        f"{len(per_file)} file(s):\n"
        + "\n".join(f"  {rel}: {count} occurrence(s) of {lits}"
                    for rel, count, lits in per_file[:10])
    )


# --------------------------------------------------------------------------
# assertions: is the rewritten program the one that runs?
# --------------------------------------------------------------------------

def test_the_command_package_is_where_the_build_looks_for_it(repo):
    """``app.go`` at the module root is what every build in this repository names.

    The Dockerfile runs ``go build -o /go/bin/app`` from the module root and the
    CI workflows do the same; there is no ``cmd/`` directory and no Makefile.  So
    ``package main`` at the root is not a convention here, it is the build.
    """
    path = repo / "app.go"
    assert path.is_file(), (
        "app.go is absent from the module root; the Dockerfile's "
        "`go build -o /go/bin/app` builds the root package and there is no other "
        "command directory in this repository"
    )
    text = srbscan.read(path)
    assert re.search(r"^package\s+main\b", text, re.M), (
        "app.go does not declare `package main`; nothing at the module root can "
        "build into a program"
    )


def test_report_how_the_package_layout_changed(repo, original):
    """Which files ``pkg/`` gained or lost, as an observation.

    Reported because every finding in this stage cites a path: a reviewer reading
    ``pkg/server.go:83: not found`` should already know whether the tree was
    rearranged.

    A REPORT and not an assertion, which is a correction rather than a style
    choice.  Written as `assert not gained` this fails a submission that put its
    dispatcher in a new file -- and for this migration that is not merely allowed,
    it is the tidiest available answer: the string-prefix rule needs perhaps ninety
    lines of router, and a reviewer would rather read them in `pkg/router.go` than
    interleaved with the handlers.  Measured against a working port, which added
    exactly that file and was reported as having a defect.

    Only an absent `pkg/` is a real finding, and it is a finding about this
    module's own ability to look rather than about the migration: every path
    `dispatch` cites is relative to a tree it could not walk.
    """
    def names(tree):
        base = tree / "pkg"
        return sorted(p.name for p in base.iterdir()) if base.is_dir() else []

    before, after = names(original), names(repo)
    if not after:
        pytest.fail("pkg/ is absent from the delivered tree; the original has "
                    f"{len(before)} file(s) there: {before}. Every path this "
                    f"module reports is relative to a layout that no longer holds")
    lost = [n for n in before if n not in after]
    gained = [n for n in after if n not in before]
    if not (lost or gained):
        return
    pytest.fail("REPORT (not a defect): pkg/ was rearranged"
                + (f"; gone: {lost}" if lost else "")
                + (f"; added: {gained}" if gained else "")
                + ". Adding a file here is the expected shape for this migration; "
                  "a file that disappeared is worth reading about in the diff.")


def test_the_go_directive_supports_the_target(repo, original):
    """ServeMux's method patterns and ``{name...}`` wildcards arrived in Go 1.22.

    Below that directive the destination the task names does not exist: ``mux.Handle
    ("GET /files/", h)`` registers a *literal pattern containing a space* and
    matches nothing.  A submission that lowered the directive has either not used
    the feature or has used it in a tree that cannot compile it, and both are worth
    knowing before reading the registration table.

    Raising the directive is fine.  Lowering it below the original's is the
    symptom, and lowering it below 1.22 is disqualifying on its own terms.
    """
    def directive(tree):
        for line in srbscan.gomod(tree).splitlines():
            m = re.match(r"^go\s+(\S+)", line.strip())
            if m:
                return m.group(1)
        return None

    before, after = directive(original), directive(repo)
    if before is None or after is None:
        pytest.fail(f"missing go directive: original={before!r} submission={after!r}")

    def key(v):
        return tuple(int(p) for p in re.findall(r"\d+", v))

    problems = []
    if key(after) < key(before):
        problems.append(f"the language directive moved backwards: {before} -> {after}")
    if key(after) < (1, 22):
        problems.append(
            f"the language directive is {after}, below the 1.22 that introduced "
            "ServeMux method patterns and {name...} wildcards"
        )
    assert not problems, "; ".join(problems)


def test_the_container_contract_is_unchanged(repo, original):
    """The image builds a binary and runs it; neither line is the task's business.

    Two things in this Dockerfile matter and nothing else does: the ``go build``
    that produces the binary, and the ``ENTRYPOINT`` that runs it.  Comparing the
    whole file would report reformatting as a finding, so this compares those.
    A submission that pointed the build at a different package while leaving a
    correct rewrite elsewhere in the tree would be measured on the wrong program.
    """
    def salient(tree):
        path = tree / "Dockerfile"
        if not path.is_file():
            return None
        out = {}
        for line in srbscan.read(path).splitlines():
            s = line.strip()
            if s.upper().startswith("ENTRYPOINT"):
                out["entrypoint"] = s
            elif "go build" in s:
                out["build"] = s
            elif s.upper().startswith("COPY") and "/go/bin/" in s:
                out["copy"] = s
        return out

    before, after = salient(original), salient(repo)
    if after is None:
        pytest.fail("no Dockerfile in the delivered tree")
    diffs = [
        f"{k}: {before.get(k)!r} -> {after.get(k)!r}"
        for k in ("build", "copy", "entrypoint")
        if before.get(k) != after.get(k)
    ]
    assert not diffs, "the container contract changed:\n" + "\n".join(
        f"  {d}" for d in diffs
    )


def test_the_cli_surface_is_unchanged(repo, original):
    """Every flag the behavioural stage sets has to still exist under its own name.

    The comparison corpus runs the binary under four flag profiles -- default,
    CORS off, auth on with two token lists, and a 16-byte upload cap -- so a
    renamed flag turns into a server that refuses to start and a module that fails
    for a reason no reviewer would guess from the message.

    Read out of the flag declarations rather than from a list written here, so
    adding a flag is not a finding and dropping one is.
    """
    decl = re.compile(r'\b(?:flags|fs|f|flag)\.(?:String|Bool|Int|Int64|Uint|'
                      r'Float64|Duration|Var|StringSlice|StringArray)'
                      r'(?:Var)?P?\s*\(\s*(?:&\w+(?:\.\w+)*\s*,\s*)?"([^"]+)"')

    def flags(tree):
        found = set()
        for path, _rel in srbscan.go_sources(tree):
            found.update(decl.findall(srbscan.read(path)))
        return found

    before, after = flags(original), flags(repo)
    if not before:
        pytest.fail("no flag declarations found in the original; this check needs "
                    "recalibrating against the tree it is reading")
    lost = sorted(before - after)
    assert not lost, (
        f"{len(lost)} command-line flag(s) the original declared are no longer "
        f"declared: {lost}"
    )


def test_no_build_tag_selects_between_two_implementations(repo):
    """The original tree has no build constraints at all.

    So any that appear are new, and a build constraint is the one mechanism that
    lets two versions of the same function coexist in a package while only one
    reaches the compiler -- which would make the tree the reviewer reads and the
    binary the behavioural stage measures different programs.
    """
    hits = []
    for path, rel in srbscan.go_files(repo):
        for i, line in enumerate(srbscan.read(path).splitlines()[:20], 1):
            if re.match(r"^//\s*(go:build|\+build)\b", line):
                hits.append(f"{rel}:{i}: {line.strip()}")
    assert not hits, (
        f"{len(hits)} build constraint(s) in a tree that had none:\n"
        + "\n".join(f"  {h}" for h in hits[:10])
    )


_LEGACY_NAME = re.compile(r"(_mux|mux_|_gorilla|_old|_legacy|_backup|_orig|"
                          r"\.orig$|\.bak$|_v1$|_before$)", re.I)


def test_no_preserved_copy_of_the_retired_implementation(repo):
    """A kept-just-in-case copy is dead weight at best.

    At worst it is what the program actually runs, selected by a flag or an
    environment variable somewhere else in the tree.  Filename heuristics are
    crude, so this is one of the weaker checks here -- but the names it looks for
    are ones nobody picks on purpose in a finished port.
    """
    hits = []
    for _path, rel in srbscan.go_files(repo):
        stem = rel.rsplit("/", 1)[-1].removesuffix(".go")
        if _LEGACY_NAME.search(stem):
            hits.append(rel)
    for path in repo.rglob("*"):
        rel = str(path.relative_to(repo))
        if srbscan.is_exempt(rel):
            continue
        if path.is_dir() and _LEGACY_NAME.search(path.name):
            hits.append(rel + "/")
    assert not hits, (
        f"{len(hits)} path(s) look like a preserved copy of the old "
        f"implementation: {sorted(hits)[:10]}"
    )


def test_no_committed_build_artefact(repo):
    """A compiled binary in the tree is a smell with one specific cause.

    The behavioural stage builds the submission from source in an offline image.  A
    committed binary is a binary nobody watched being produced, and on this task it
    could have been built anywhere, against anything -- including against mux.
    """
    hits = []
    for name in ("bin", "dist", "_dist", "testbin", "app"):
        base = repo / name
        if base.is_dir():
            files = [p for p in base.rglob("*") if p.is_file()]
            hits.append(f"{name}/ ({len(files)} file(s))")
        elif base.is_file() and base.stat().st_mode & 0o111:
            hits.append(f"{name} (executable, {base.stat().st_size} bytes)")
    assert not hits, f"the delivered tree carries build output: {hits}"

