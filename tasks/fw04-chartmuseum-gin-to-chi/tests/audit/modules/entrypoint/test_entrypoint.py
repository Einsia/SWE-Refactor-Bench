"""Do the published entry points still name what they used to?

A framework migration is supposed to be invisible from outside the
repository.  The binary keeps its name, the Makefile keeps its targets, the
container keeps its ``ENTRYPOINT``, and the release scripts keep working --
none of that is what the task asks for, which is exactly why it is worth
checking: those are the surfaces a submission breaks by accident while its
attention is on the router.

Everything here compares the submission against the original rather than
against a hardcoded list.  That costs nothing -- both trees are mounted --
and it means these checks describe the *delta* a reviewer needs to see
instead of a schema somebody wrote down once and then let drift.  Advisory
like the rest of the stage: a renamed target might be a deliberate,
defensible change, and only the reviewer can weigh that against the
instruction the submission was given.
"""

from __future__ import annotations

import re

import pytest

import srbscan

pytestmark = pytest.mark.scan


# --------------------------------------------------------------------------
# the program itself
# --------------------------------------------------------------------------

def test_the_command_package_is_where_it_was(repo, original):
    """``cmd/chartmuseum`` is the one path the build automation hardcodes.

    Every cross-compile target in the Makefile names it, so moving the
    package means either editing all of them or breaking all of them.  This
    reports which of the two happened.
    """
    missing = []
    for path in ("cmd/chartmuseum/main.go",):
        if not (repo / path).is_file():
            missing.append(path)
    assert not missing, (
        "the command package is not where the build automation looks for it: "
        f"{missing} absent from the delivered tree"
    )
    text = srbscan.read(repo / "cmd/chartmuseum/main.go")
    assert re.search(r"^package\s+main\b", text, re.M), (
        "cmd/chartmuseum/main.go does not declare `package main`; "
        "nothing in that directory can build into a program"
    )


def test_the_go_directive_did_not_move_backwards(repo, original):
    """Raising the language version is fine.  Lowering it is a symptom.

    A submission that dropped ``go.mod``'s directive usually did so to make
    some resolution problem go away, and a lower directive changes which
    language features the *original* tree would have compiled under -- which
    makes the two builds the behavioural stage compares less comparable.
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

    assert key(after) >= key(before), (
        f"go.mod's language directive moved backwards: {before} -> {after}"
    )


# --------------------------------------------------------------------------
# the automation around it
# --------------------------------------------------------------------------

_TARGET = re.compile(r"^([A-Za-z][A-Za-z0-9_.-]*):(?:\s|$)", re.M)


def _targets(tree):
    """Recipe-bearing Makefile targets.

    GNU make lets a target line carry only a variable export -- this tree
    uses that heavily, one ``export`` line per target per variable -- so a
    naive scan reports the same target five times and misses nothing else.
    Filtering to lines whose body is empty leaves the real declarations.
    """
    path = tree / "Makefile"
    if not path.is_file():
        return None
    found = []
    for line in srbscan.read(path).splitlines():
        m = _TARGET.match(line)
        if not m:
            continue
        rest = line[m.end() - 1:].lstrip(":").strip()
        if rest.startswith("export "):
            continue
        found.append(m.group(1))
    return sorted(set(found))


def test_the_makefile_still_declares_the_targets_it_used_to(repo, original):
    """The behavioural stage runs some of these.  All of them are contracts.

    A missing target is a broken workflow for whoever maintains the
    repository next, whether or not any grader happens to invoke it.  New
    targets are reported too, without prejudice: adding one is a normal
    thing to do during a migration, and the reviewer may still want to know
    a ``build-chi`` appeared beside ``build-linux``.
    """
    before, after = _targets(original), _targets(repo)
    if after is None:
        pytest.fail("no Makefile in the delivered tree")
    lost = [t for t in before if t not in after]
    gained = [t for t in after if t not in before]
    problems = []
    if lost:
        problems.append(f"targets that disappeared: {lost}")
    if gained:
        problems.append(f"targets that appeared: {gained}")
    assert not problems, "; ".join(problems)


def test_the_container_entrypoint_is_unchanged(repo, original):
    """The image contract, which no part of the task asks to change.

    Two things matter in this file and nothing else does: the path the
    binary is copied *from* -- the Makefile writes it there -- and the
    ``ENTRYPOINT`` the container runs.  Comparing the whole file would
    report reformatting as a finding, so this compares those two lines.
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
            elif s.upper().startswith("COPY") and "chartmuseum" in s:
                out["copy"] = s
        return out

    before, after = salient(original), salient(repo)
    if after is None:
        pytest.fail("no Dockerfile in the delivered tree")
    diffs = [
        f"{k}: {before.get(k)!r} -> {after.get(k)!r}"
        for k in ("copy", "entrypoint")
        if before.get(k) != after.get(k)
    ]
    assert not diffs, "the container contract changed:\n" + "\n".join(
        f"  {d}" for d in diffs
    )


def test_the_scripts_directory_survives(repo, original):
    """``scripts/`` is what the test and release targets shell out to.

    The behavioural stage drives the repository's own test entry point, and
    that path runs through here.  A missing script turns into a build-time
    failure two stages later with a much less obvious message than this one.
    """
    def names(tree):
        base = tree / "scripts"
        return sorted(p.name for p in base.iterdir()) if base.is_dir() else []

    lost = [n for n in names(original) if n not in names(repo)]
    assert not lost, f"scripts/ lost {len(lost)} file(s): {lost}"


def test_the_top_level_layout_is_recognisable(repo, original):
    """A coarse orientation check, reported for context rather than blame.

    Directories can legitimately move during a port.  The reason to
    surface it is that every other finding in this stage cites a path, and
    a reviewer reading ``pkg/chartmuseum/router/router.go: not found``
    should already know the tree was rearranged.

    "For context rather than blame" has to be in the message, not only
    here.  The reviewer reads this through ``scan.digest``, and a bare list
    of directory names is indistinguishable from an accusation -- the same
    shape as BUG 18, where a comment-only hit reached a required gate as a
    survival because the disambiguating text sat past the truncation.
    """
    def tops(tree):
        return sorted(
            p.name for p in tree.iterdir()
            if p.is_dir() and p.name not in (".git", "_dist", "vendor")
        )

    before, after = tops(original), tops(repo)
    lost = [d for d in before if d not in after]
    gained = [d for d in after if d not in before]
    problems = []
    if lost:
        problems.append(f"top-level directories that disappeared: {lost}")
    if gained:
        problems.append(f"top-level directories that appeared: {gained}")
    assert not problems, (
        "ORIENTATION, not a violation -- directories may legitimately move "
        "during a port; this is here so a later 'path not found' finding "
        "makes sense: " + "; ".join(problems)
    )


# --------------------------------------------------------------------------
# two implementations where there should be one
# --------------------------------------------------------------------------

# Router libraries a submission might plausibly reach for.  The point is not
# to police the choice -- the instruction names the target and the reviewer
# can read it -- but to notice when *two* of them are wired at once, which
# means one of the two is dead code or, worse, a runtime fallback.
_ROUTERS = (
    "github.com/go-chi/chi",
    "github.com/gorilla/mux",
    "github.com/julienschmidt/httprouter",
    "github.com/labstack/echo",
    "github.com/gofiber/fiber",
    "github.com/gin-gonic/gin",
)


def test_only_one_router_library_is_wired(repo):
    """Two routers in one tree is the shape of a hedged migration.

    Reports the full mapping either way, because "which library ended up
    carrying dispatch, and from where" is the first thing the reviewer wants
    and is otherwise several greps away.
    """
    wired = {}
    for module in _ROUTERS:
        hits = srbscan.importers(repo, module, tests=False)
        if hits:
            wired[module] = sorted(hits)
    assert len(wired) <= 1, "more than one router library is imported by delivered source:\n" + "\n".join(
        f"  {module} <- {', '.join(files)}" for module, files in sorted(wired.items())
    )


_LEGACY_NAME = re.compile(
    r"(_gin|gin_|_old|_legacy|_backup|_orig|\.orig$|\.bak$|_v1$)", re.I
)


def test_no_preserved_copy_of_the_retired_implementation(repo):
    """A kept-just-in-case copy is dead weight at best.

    At worst it is what the program actually runs, selected by a flag or an
    environment variable somewhere else in the tree.  Filename heuristics
    are crude, so this is one of the weaker checks here -- but the names it
    looks for are ones nobody picks on purpose in a finished port.
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
        f"{len(hits)} path(s) look like a preserved copy of the old implementation: "
        f"{sorted(hits)[:10]}"
    )


def test_no_build_tag_selects_between_two_implementations(repo):
    """The original tree has no build constraints at all.

    So any that appear are new, and a build constraint is the one mechanism
    that lets two versions of the same function coexist in a package while
    only one reaches the compiler -- which would make the tree the reviewer
    reads and the binary the behavioural stage measures different programs.
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


def test_no_committed_build_artefact(repo):
    """Compiled output in the tree is a smell with one specific cause.

    The Makefile writes binaries into ``_dist/``; the container copies from
    there.  A committed ``_dist`` means a binary the submission did not
    build in front of the grader could end up being the one that runs.
    """
    hits = []
    for name in ("_dist", "bin", "testbin"):
        base = repo / name
        if base.is_dir():
            files = [p for p in base.rglob("*") if p.is_file()]
            hits.append(f"{name}/ ({len(files)} file(s))")
    assert not hits, f"the delivered tree carries build output: {hits}"
