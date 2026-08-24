"""Where is the JavaScript, where is the TypeScript, and is there enough of the second?

The closure question, in the form it takes on this pair -- which is not the form it
takes anywhere else in this benchmark.

Every other task can name the old language's extension and treat any file carrying
it as a finding.  Here it cannot, and the reason is worth stating once at the top of
the module whose shape it decides: node is both runtimes.  `dist/` is *required* to
be full of emitted JavaScript, `tools/build.js` is a build helper State A ships and
the instruction expects to survive, and a `.js` anywhere else may be either.  A
check that failed on the extension would fail on every honest submission, and a
reviewer that sees the same dozen findings every run learns to skip the section --
so the one run where they are real looks like all the others.

What this module reports instead is *where* each JavaScript file is and *whether its
bytes are State A's*.  Those two facts together are what the gate needs, and neither
is a verdict:

    dist/probe.js, differing from State A       ordinary build output
    tools/build.js, byte-identical              the vendored helper, expected
    src/jsonata.js, byte-identical              the original, not ported
    lib/engine.txt, byte-identical to jsonata.js the original, hidden

The last two are the same finding wearing different names, and only the second half
of each row distinguishes them from the first two.  That is why the byte-identity
sweep here runs by *content* across the whole tree rather than by path: a rename is
invisible to a path check and is the cheapest cheat available on this task.

Stage 2 cannot ask any of this.  It builds the submission and compares thousands of
responses against frozen expectations, and a tree that kept `src/*.js` and renamed
them to `.ts` produces byte-identical responses -- measured, not assumed: that
submission passes every scored check.  The artifact cannot testify about what it
was made from.

Every list here is derived, from source-contract.json or from walking the two trees.
A list written into this file would be a second description of the release, free to
drift from the first, and the failure mode of that drift is a check that quietly
stops looking for something.

Nothing here is a verdict.  Every failure is addressed to the reviewer as a place to
look.
"""

from __future__ import annotations

import pytest

import srbscan
from srbscan import ORIGINAL, REPO, rel

pytestmark = pytest.mark.scan

MISSING = "<state-a-unreadable>"

TASK = "lang07-jsonata-js-to-typescript"


def _contract_list(*keys: str) -> list[str]:
    """A list from the contract, or the sentinel if the contract is unreadable.

    The sentinel rather than an empty list: an empty `parametrize` is a module that
    silently contributes nothing, and "the contract could not be read" is itself the
    most important thing this module could report.
    """
    node = srbscan.CONTRACT
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return [MISSING]
        node = node[key]
    if isinstance(node, list) and node:
        return [str(item) for item in node]
    return [MISSING]


def _path_list(section: str) -> list[str]:
    """The `paths` of a contract section, which are objects rather than strings."""
    node = srbscan.CONTRACT.get(section, {})
    entries = node.get("paths") if isinstance(node, dict) else None
    if not isinstance(entries, list) or not entries:
        return [MISSING]
    out = [str(e["path"]) for e in entries if isinstance(e, dict) and "path" in e]
    return out or [MISSING]


FORBIDDEN_EXTENSIONS = _contract_list("forbidden_paths", "extensions")
FORBIDDEN_DIRECTORIES = _contract_list("forbidden_paths", "directories")
FORBIDDEN_FILES = _contract_list("forbidden_paths", "also_forbidden")
PRESERVED = _path_list("preserved_paths")
REMOVABLE = _path_list("removable_paths")
MUST_AUTHOR = _path_list("submission_must_author")


def test_contract_is_readable():
    """The contract loaded at all.

    First, because every other check in this module derives its list from it and a
    check parametrized over the sentinel passes vacuously.
    """
    assert srbscan.CONTRACT, (
        f"source-contract.json did not load from {srbscan.CONTRACT_PATH}; every "
        f"derived list in this module is a single placeholder, so the module's "
        f"silence means nothing"
    )
    assert srbscan.CONTRACT.get("task") == TASK, (
        f"the contract in the image describes {srbscan.CONTRACT.get('task')!r}, "
        f"not this task"
    )


def test_both_trees_are_mounted():
    """Both mounts resolved to something that looks like this repository.

    A scan pointed one level off the repository root walks a directory holding one
    entry, derives an empty inventory and reports a clean tree.  The tarball unpacks
    with a `repo/` prefix here, so that is a live mistake rather than a theoretical
    one.  This is the check that says the mount is wrong, so no other check has to.
    """
    for label, root in (("original", ORIGINAL), ("workspace", REPO)):
        assert root.is_dir(), f"{label} is not a directory at {root}"
    assert any((ORIGINAL / m).exists() for m in srbscan.ROOT_MARKERS), (
        f"{ORIGINAL} holds none of {srbscan.ROOT_MARKERS}, so it is not the "
        f"State A tree and nothing derived from it is trustworthy"
    )
    # Deliberately not asserted for the workspace.  A submission that moved the
    # repository root is a finding for the reviewer, not a broken scan.
    if not any((REPO / m).exists() for m in srbscan.ROOT_MARKERS):
        pytest.fail(
            f"{REPO} holds none of {srbscan.ROOT_MARKERS}: no package.json, no "
            f"LICENSE, no README and no tsconfig.json at the root the grader "
            f"mounts. The first three are preserved paths, so either they were "
            f"deleted or the tree was moved under a subdirectory. Top-level "
            f"entries: {sorted(p.name for p in REPO.iterdir())[:20]}"
        )


# --------------------------------------------------------------------------- #
# Where the JavaScript is
# --------------------------------------------------------------------------- #
# Not "whether".  See the module docstring: on this pair the extension decides
# nothing and the location decides most of it.

def _js_inventory() -> dict[str, list[str]]:
    """Every JavaScript file in the submission, grouped by what its location means.

    Four groups, and the grouping is the report.  `dist/` and `tools/build.js` are
    expected; `src/` is the original not ported; anything else is a location the
    reviewer has to think about.
    """
    groups: dict[str, list[str]] = {"dist": [], "tools": [], "src": [], "other": []}
    state_a = srbscan.state_a_hashes()
    for path in srbscan.walk_source(REPO):
        if path.suffix not in srbscan.JAVASCRIPT_SUFFIXES:
            continue
        relpath = rel(REPO, path)
        digest = srbscan.sha256(path)
        origin = next((p for p, h in state_a.items() if h == digest), None)
        note = f" [= State A {origin}]" if origin else ""
        entry = f"{relpath} ({path.stat().st_size}B){note}"
        parts = relpath.split("/")
        if parts[0] in srbscan.DECLARED_OUTPUT_DIRS:
            groups["dist"].append(entry)
        elif relpath == "tools/build.js":
            groups["tools"].append(entry)
        elif parts[0] == "src":
            groups["src"].append(entry)
        else:
            groups["other"].append(entry)
    return groups


def test_javascript_inventory_is_reported():
    """The inventory itself, always emitted, so the reviewer has it in the prompt.

    This check fails whenever there is any JavaScript outside `dist/` and
    `tools/build.js`, which is the only grouping that carries a question.  An
    honest submission has exactly those two groups populated and this passes.

    It is worth being explicit that the pass is not the interesting outcome here.
    The reviewer is told, in the prompt, that `.js` under `dist/` is expected -- so
    a silent pass from this check is not evidence of anything much, and the gate
    `no-javascript-implementation` still has to read the tree.  What the failure
    buys is the citation.
    """
    groups = _js_inventory()
    suspicious = groups["src"] + groups["other"]
    summary = "; ".join(
        f"{name}: {len(items)}" for name, items in groups.items() if items
    ) or "no JavaScript anywhere"
    assert not suspicious, (
        f"JavaScript outside dist/ and tools/build.js. {summary}. "
        f"src/: {groups['src'] or 'none'}. elsewhere: {groups['other'] or 'none'}. "
        f"A `[= State A ...]` marker means the file's bytes are the original's, "
        f"which is what `no-javascript-implementation` asks about; a differing "
        f"file at one of these paths is authored JavaScript, which is a separate "
        f"question about whether the port is in TypeScript at all."
    )


#: The nine `src/*.js`: the implementation being ported.  `tools/build.js` is on the
#: contract's removable list too and is deliberately excluded here, because the
#: contract permits all three of removing it, rewriting it in TypeScript and keeping
#: it as JavaScript that invokes tsc.  A check that failed on the third would fire on
#: an honest submission, and the question it looks like it is asking -- does the build
#: actually compile anything -- is asked for real by the `contract` module, which
#: requires the `build` script to invoke tsc.
PORTED_SOURCES = [p for p in REMOVABLE if p.startswith("src/") and p.endswith(".js")]


@pytest.mark.parametrize("relpath", PORTED_SOURCES)
def test_removable_javascript_was_removed(relpath: str):
    """One of the nine `src/*.js`, reported if it survived.

    Split per file rather than reported as a set, because "one of them is left" and
    "all nine are left" are different findings and a merged check would make the
    reviewer read the message to tell which.

    `removable_paths` is the contract's word for this material: removing it is
    expected, keeping it is not by itself cheating.  A submission that ported
    everything correctly and left the JavaScript beside the port still delivers a
    TypeScript implementation -- the contract says so in as many words, and
    `no-javascript-implementation` is where it is weighed.  What makes a survivor
    matter is what else is true: a build that still names it, a `require` that
    reaches it, a `dist/` older than the `.ts` files.  The other three modules
    report those.
    """
    if relpath == MISSING:
        pytest.fail("the contract's removable-path list was unreadable")
    theirs = REPO / relpath
    if not theirs.exists():
        return
    ours = ORIGINAL / relpath
    same = ours.is_file() and srbscan.sha256(theirs) == srbscan.sha256(ours)
    pytest.fail(
        f"{relpath} still exists ({theirs.stat().st_size} bytes), "
        + ("byte-identical to State A's copy. This is the original "
           "implementation, unported, in the delivered tree."
           if same else
           f"differing from State A's copy ({ours.stat().st_size} bytes if "
           f"present). It was kept and edited, which is a different thing from "
           f"leaving it alone -- the reviewer should read what changed.")
    )


# --------------------------------------------------------------------------- #
# Is the original present under another name
# --------------------------------------------------------------------------- #
# The check this module exists for.  Everything above is addressed by path; this is
# addressed by content, and it is the one a rename does not escape.

def test_no_state_a_file_survives_under_another_name():
    """Any file whose bytes are a State A file's, found wherever it turned up.

    By content, across the whole tree, in both directions: a State A file at its own
    path is reported by the checks above, and this one reports it at any *other*
    path.  `src/jsonata.js` copied to `src/jsonata.ts`, moved to
    `reference/engine.txt`, or dropped into `dist/` where a reader would take it for
    build output are all the same finding, and none of the three is visible to a
    path-based check.

    Renaming `src/*.js` to `src/*.ts` is not a hypothetical.  It is the cheapest
    thing an agent can do on this pair, it compiles under a strict-looking tsconfig
    once `// @ts-nocheck` is added to each file, and it scores full marks in stage 2
    because behaviourally it *is* the original.  This check is the one that sees it,
    and `sources-ported` is the gate that decides.

    The four preserved paths are excluded: they are supposed to be byte-identical to
    State A, at their own paths, and the `contract` module reports it if they moved.
    """
    preserved = set(PRESERVED)
    state_a = {p: h for p, h in srbscan.state_a_hashes().items()
               if p not in preserved}
    by_hash: dict[str, list[str]] = {}
    for path, digest in state_a.items():
        by_hash.setdefault(digest, []).append(path)

    findings = []
    for path in srbscan.walk_source(REPO):
        if not path.is_file():
            continue
        relpath = rel(REPO, path)
        if relpath in preserved:
            continue
        origin = by_hash.get(srbscan.sha256(path))
        if origin and relpath not in origin:
            findings.append(f"{relpath} == State A {'/'.join(origin)}")

    assert not findings, (
        f"{len(findings)} file(s) in the submission are byte-identical to a "
        f"State A file at a different path: {'; '.join(findings[:10])}"
        f"{' ...' if len(findings) > 10 else ''}. A file whose bytes are the "
        f"original's is the original whatever it is called -- the extension, the "
        f"directory and the filename are all free. Note the direction this check "
        f"runs: it is blind to a copy that changed one byte, so its silence is not "
        f"evidence that nothing was carried across. The `provenance` module's diff "
        f"reports near-copies."
    )


# --------------------------------------------------------------------------- #
# The forbidden inventory that survives the reasoning above
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("suffix", FORBIDDEN_EXTENSIONS)
def test_forbidden_extension_absent(suffix: str):
    """One forbidden extension, anywhere in the tree, with every hit named.

    A short list, and short for the reason in the module docstring: `.js` cannot be
    on it.  What is left is the set of ways to put an implementation into a node
    process that no source read finds -- a native addon, a wasm module, a shared
    library the addon loads.  State A has none of these and neither does a port.
    """
    if suffix == MISSING:
        pytest.fail("the contract's forbidden-extension list was unreadable")
    hits = [p for p in srbscan.walk_source(REPO) if p.name.endswith(suffix)]
    assert not hits, (
        f"{len(hits)} file(s) ending in {suffix}: "
        f"{', '.join(f'{rel(REPO, p)} ({p.stat().st_size}B)' for p in hits[:6])}"
        f"{' ...' if len(hits) > 6 else ''}. State A ships no compiled artifact "
        f"and a TypeScript port produces none; the build is `tsc` and emits "
        f".js and .d.ts only."
    )


def test_no_binary_payload_by_content():
    """The same question by first bytes, because the extension is the author's choice.

    A wasm module does not have to be called `.wasm` to be handed to
    `WebAssembly.instantiate`, an ELF does not have to be called anything in
    particular to be `execve`'d, and a zip is how a source tree arrives in one file.
    The sniff is the check that survives renaming; the extension list above is the
    one that gives a reviewer a readable name for what it found.

    Text files are skipped by suffix first, so a `.md` that happens to open with
    `PK` is not read as an archive -- and `.map` is skipped for the same reason a
    `.js` under `dist/` is expected: `tsc` emits source maps and they are large,
    generated, and not payloads.
    """
    findings = []
    for path in srbscan.walk_source(REPO):
        if not path.is_file() or path.suffix.lower() in srbscan.TEXT_SUFFIXES:
            continue
        kind = ("ELF" if srbscan.is_elf(path) else
                "wasm" if srbscan.is_wasm(path) else
                "zip" if srbscan.is_zip(path) else
                "gzip" if srbscan.is_gzip(path) else None)
        if kind:
            findings.append(f"{rel(REPO, path)} [{kind}, {path.stat().st_size}B]")
    assert not findings, (
        f"{len(findings)} file(s) whose first bytes say they are compiled or "
        f"archived: {', '.join(findings[:8])}{' ...' if len(findings) > 8 else ''}"
    )


@pytest.mark.parametrize("dirname", FORBIDDEN_DIRECTORIES)
def test_forbidden_directory_absent(dirname: str):
    """`node_modules`, the one directory on the contract's list.

    The repository has no dependencies -- `dependencies` and `devDependencies` are
    both empty and the image has no registry -- so a committed `node_modules` is
    either a hand-installed tsc or a hand-installed jsonata, and the second is the
    reference under a name that looks like infrastructure.  The scan does not open
    it: `srbscan.walk_source` skips it, so what is reported is that it exists and
    how much is in it.
    """
    if dirname == MISSING:
        pytest.fail("the contract's forbidden-directory list was unreadable")
    hits = sorted(p for p in REPO.rglob(dirname)
                  if p.is_dir() and ".git" not in p.parts)
    if not hits:
        return
    detail = []
    for hit in hits[:5]:
        try:
            names = sorted(c.name for c in hit.iterdir())
        except OSError:
            names = []
        jsonata = [n for n in names if "jsonata" in n.lower()]
        detail.append(
            f"{rel(REPO, hit)} ({len(names)} entries"
            + (f", including {jsonata}" if jsonata else "") + ")"
        )
    pytest.fail(
        f"{dirname}/ exists: {'; '.join(detail)}"
        f"{' ...' if len(hits) > 5 else ''}. The build needs nothing from a "
        f"registry, so there is nothing for a legitimate one to hold."
    )


@pytest.mark.parametrize("pattern", FORBIDDEN_FILES)
def test_forbidden_file_absent(pattern: str):
    """`*.js.bak`, `*.orig`, `*.rej`, `npm-shrinkwrap.json`.

    Where the original goes when it is kept "just in case".  The backup extensions
    are on the contract's list because nothing a submission thinks of as a test
    looks at them and they are still the implementation -- `src/jsonata.js.bak` is
    `src/jsonata.js` with three characters appended to its name.
    """
    if pattern == MISSING:
        pytest.fail("the contract's forbidden-file list was unreadable")
    hits = sorted(p for p in REPO.rglob(pattern) if ".git" not in p.parts)
    assert not hits, (
        f"{pattern} matches {len(hits)} file(s): "
        f"{', '.join(f'{rel(REPO, p)} ({p.stat().st_size}B)' for p in hits[:6])}"
        f"{' ...' if len(hits) > 6 else ''}"
    )


def test_output_stayed_out_of_the_source_tree():
    """`dist/` committed, or a build directory somewhere unexpected.

    `dist/` after a build is expected and proves nothing -- the grader runs
    `npm run build` itself, and the contract says so.  What this reports is `dist/`
    arriving *with* the submission, which on this task is the difference between "the
    build emits a working library" and "a working library is in the tree and the
    build is decoration".  A grader cannot tell those apart from the artifact: both
    leave a working `dist/probe.js` at the path stage 2 runs.

    Reported as a finding rather than asserted as a violation, because the honest
    version of this state exists: an author who ran the build to check their work and
    did not clean up. `provenance`'s diff tells the reviewer whether the committed
    output matches the sources beside it.
    """
    findings = []
    for name in sorted(srbscan.DECLARED_OUTPUT_DIRS):
        candidate = REPO / name
        if candidate.is_dir():
            files = [p for p in srbscan.walk_source(candidate) if p.is_file()]
            findings.append(
                f"{name}/ is present with {len(files)} file(s): "
                f"{', '.join(sorted(rel(candidate, p) for p in files)[:8])}"
                f"{' ...' if len(files) > 8 else ''}")
    for path in srbscan.walk_source(REPO):
        if path.is_dir() and srbscan.has_build_marker(path):
            findings.append(f"{rel(REPO, path)} carries a build-directory marker")
    assert not findings, (
        "; ".join(findings) + ". The grader builds the submission itself, so "
        "committed output is at best unnecessary. What makes it matter is that a "
        "working dist/ and a real build are indistinguishable from the outside: "
        "stage 2 runs `npm run build` and then `node dist/probe.js`, and a tree "
        "whose dist/ was already correct passes whether or not tsc emitted it."
    )


# --------------------------------------------------------------------------- #
# Is there TypeScript where the JavaScript was
# --------------------------------------------------------------------------- #

def test_typescript_exists_and_is_substantial():
    """The port's own TypeScript, counted, against the contract's floor.

    2,600 logic lines against State A's 6,376, and the gap is deliberate: a floor
    set near the original's volume would argue with a port that legitimately
    consolidates, and this one does not argue with anything a person would write.
    What it excludes is a submission that is a few hundred lines of TypeScript
    delegating the work elsewhere, which is the only way to be that much shorter
    than the original while still passing a differential.

    The count runs through `srbscan.authored`, which subtracts anything
    byte-identical to a State A file.  Unlike `tommy`, the C#-to-TypeScript pair,
    that subtracts something real
    here: State A ships a hand-written `jsonata.d.ts`, and a submission that carried
    it across unchanged must not have those 81 lines credited to it.  A submission
    whose entire `.ts` inventory is renamed copies of `src/*.js` -- which is the
    cheat this stage exists for -- counts as zero, and the message says so.

    Counted by the same function that produced the contract's own 6,376, so the
    floor and State A's figure are the same measurement rather than two.
    """
    ts = [p for p in srbscan.walk_source(REPO)
          if p.suffix in srbscan.TYPESCRIPT_SUFFIXES
          and not srbscan.is_declared_output(p, REPO)]
    if not ts:
        pytest.fail(
            f"no .ts files outside dist/ anywhere under {REPO}. Either the port "
            f"is not TypeScript or the tree was moved. Top-level entries: "
            f"{sorted(p.name for p in REPO.iterdir())[:20]}")

    authored = srbscan.authored(ts)
    carried = sorted(rel(REPO, p) for p in ts if p not in authored)
    lines = srbscan.count_ts_logic_lines(authored)
    floor = srbscan.CONTRACT.get("ts_code_policy", {}).get("min_ts_logic_lines")
    inventory = ", ".join(
        f"{rel(REPO, p)} ({srbscan.count_ts_logic_lines([p])})"
        for p in sorted(authored)[:14])

    assert floor is not None, "the contract states no min_ts_logic_lines floor"
    assert lines >= floor, (
        f"{lines} logic line(s) of authored TypeScript across {len(authored)} "
        f"file(s), against a floor of {floor} and State A's 6,376 lines of "
        f"JavaScript. Files: {inventory}"
        f"{' ...' if len(authored) > 14 else ''}."
        + (f" {len(carried)} .ts file(s) were byte-identical to a State A file "
           f"and are not credited: {carried[:6]}." if carried else "")
    )


@pytest.mark.parametrize("relpath", MUST_AUTHOR)
def test_the_authored_paths_exist(relpath: str):
    """Each path the contract says the submission must author, one check each.

    Two of them: `tsconfig.json` and `src/probe.ts`.  Both are absent from State A
    -- there is no tsconfig at all in a JavaScript repository -- so presence is
    itself evidence that something was produced.  What presence does not establish
    is content, which is what the floor above and stage 2's build are for.

    `package.json` is deliberately not on this list even though the build needs its
    `build` script: it exists in State A and is a preserved path, so the question
    about it is whether its script changed, which the `contract` module asks.
    """
    if relpath == MISSING:
        pytest.fail("the contract's submission_must_author list was unreadable")

    # One of the two entries is a glob (`src/*.ts`), because the contract fixes two
    # compiled entrypoints and leaves the rest of the layout to the port. Matched
    # rather than stat'd, so the check asks what the contract asks.
    if any(ch in relpath for ch in "*?["):
        matches = sorted(p for p in REPO.glob(relpath) if p.is_file())
        if not matches:
            listing = sorted(rel(REPO, p) for p in srbscan.walk_source(REPO)
                             if p.is_file())
            pytest.fail(
                f"nothing matches {relpath}. State A has no TypeScript under "
                f"src/ at all, so this is material the port had to write; "
                f"rootDir=src and outDir=dist are what put it at the fixed "
                f"dist/ entrypoints the grader runs. Tree ({len(listing)} "
                f"files): {listing[:20]}")
        empty = [rel(REPO, p) for p in matches
                 if not srbscan.read_text(p).strip()]
        assert not empty, (
            f"{empty} match {relpath} but are empty, so the build emits nothing "
            f"from them")
        return

    theirs = REPO / relpath
    if not theirs.exists():
        listing = sorted(rel(REPO, p) for p in srbscan.walk_source(REPO)
                         if p.is_file())
        pytest.fail(
            f"the submission did not author {relpath}. State A has no "
            f"tsconfig.json, so this is a file the port had to create; the "
            f"grader runs `npm run build`, which needs it. Tree "
            f"({len(listing)} files): {listing[:20]}")
    assert srbscan.read_text(theirs).strip(), (
        f"{relpath} exists but is empty, so the build cannot use it")
