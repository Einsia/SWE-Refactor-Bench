"""Did the JavaScript leave, and is there Rust where it was?

The closure question, in the several forms it takes on this task.  Stage 2 builds the
two binaries and compares 16,049 behavioural cases against a node reference; none of
those cases can tell you that `acorn/src/expression.js` is still sitting in the tree
beside the port, because a repository that kept all 77 JavaScript files *and* wrote a
complete Rust implementation builds a perfectly good pair of binaries.  The artifact
cannot testify about what was left behind.

Every list here is derived: the forbidden extensions and names come from
source-contract.json, the retained paths come from the same place, and the file
inventory comes from walking `/opt/original`.  A list written in this file would be a
second description of the upstream release, free to drift from the first, and the
failure mode of that drift is a check that quietly stops looking for something.

Nothing here is a verdict.  Every failure is addressed to the reviewer as a place to
look: this module cannot tell a vendored swc from an honest port, and it does not
try to.

The one check that can skip carries `srb_skip_ok`, and the marker is load-bearing:
`swerefactor.pytest_module` rewrites an unlicensed skip into a *fail*, so without it
`test_rust_is_the_bulk_of_the_code` reports a failure on a tree with no Rust at all --
which is the one shape where the comparison it makes has no meaning.  The absence
itself is not lost by licensing the skip; `test_rust_sources_exist` reports it as a
finding of its own, and a reviewer reading two failures for one fact would weigh it
twice.
"""

from __future__ import annotations

import re

import pytest

import srbscan
from srbscan import ORIGINAL, REPO, rel

pytestmark = pytest.mark.scan

MISSING = "<state-a-unreadable>"


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


FORBIDDEN_EXTENSIONS = _contract_list("forbidden_paths", "extensions")
FORBIDDEN_NAMES = _contract_list("forbidden_paths", "names")
RETAINED = _contract_list("retained_paths", "required")
ANCHORS = _contract_list("state_a", "anchor_files")


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
    assert srbscan.CONTRACT.get("task") == "lang04-acorn-js-to-rust", (
        f"the contract in the image describes "
        f"{srbscan.CONTRACT.get('task')!r}, not this task"
    )


def test_both_trees_are_mounted():
    """Both mounts resolved to something that looks like acorn.

    A scan pointed one level off the repository root walks a directory holding one
    entry, derives an empty list of JavaScript files and reports a clean tree.  This
    is the check that says the mount is wrong, so that no other check has to.
    """
    for label, root in (("original", ORIGINAL), ("workspace", REPO)):
        assert root.is_dir(), f"{label} is not a directory at {root}"
    assert any((ORIGINAL / m).exists() for m in srbscan.ROOT_MARKERS), (
        f"{ORIGINAL} holds none of {srbscan.ROOT_MARKERS}, so it is not the "
        f"State A tree and nothing derived from it is trustworthy"
    )
    # Deliberately not asserted for the workspace.  A submission that moved the
    # repository root is a finding for the reviewer, not a broken scan, and the next
    # clause is the one that reports it.
    if not any((REPO / m).exists() for m in srbscan.ROOT_MARKERS):
        pytest.fail(
            f"{REPO} holds none of {srbscan.ROOT_MARKERS}: no README.md, no AUTHORS "
            f"and none of the three package directories at the root the grader "
            f"mounts. README.md and AUTHORS are retained paths, so either they were "
            f"deleted or the tree was moved under a subdirectory. Top-level "
            f"entries: {sorted(p.name for p in REPO.iterdir())[:20]}"
        )


def test_state_a_inventory_is_visible():
    """State A's own file count, as walked.

    The contract says 116 files.  If the mount holds materially fewer, every
    `authored`-scoped search in this suite silently widens to the whole tree -- a
    file with no upstream counterpart is authored by definition -- and the greps this
    task most needs to scope start matching acorn's own vocabulary.  That failure is
    invisible in the findings, so it is asserted here instead.
    """
    stated = (srbscan.CONTRACT.get("state_a") or {}).get("file_count")
    if not isinstance(stated, int):
        pytest.fail("the contract states no state_a.file_count")
    walked = len(srbscan.walk_source(ORIGINAL))
    assert walked >= stated, (
        f"{ORIGINAL} holds {walked} files against the contract's {stated}. The "
        f"State A mount is incomplete, so `authored` cannot tell submitted text "
        f"from upstream text and every text finding in this suite is unscoped."
    )


# --------------------------------------------------------------------------- #
# The JavaScript should be gone
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("relpath", ANCHORS)
def test_anchor_file_is_gone(relpath: str):
    """One of State A's eleven anchor files, anywhere in the tree.

    Anywhere, not just at its State A path: moving `expression.js` to `legacy/` or
    `reference/` does not port it.  The search is by basename because that is what a
    move preserves, and the finding names every hit so the reviewer can tell a
    forgotten file from a directory of them.

    `package.json` is among the anchors and is the one whose absence is least about
    diligence: State A's is the npm manifest that makes the tree an npm package, and
    a submission that kept it is a submission `npm install` still works on.
    """
    if relpath == MISSING:
        pytest.fail("the contract's anchor file list was unreadable")
    name = relpath.rsplit("/", 1)[-1]
    hits = [p for p in srbscan.walk_source(REPO) if p.name == name]
    assert not hits, (
        f"{name} is still in the submission at "
        f"{', '.join(rel(REPO, p) for p in hits[:5])}. State A had it at "
        f"{relpath}; the task replaces it with Rust. Is this a file the port forgot "
        f"to remove, or is the JavaScript still the implementation?"
    )


@pytest.mark.parametrize("suffix", sorted(set(
    s for s in FORBIDDEN_EXTENSIONS if s != MISSING
) or {MISSING}))
def test_forbidden_extension_absent(suffix: str, files):
    """One forbidden extension, over the submitted tree.

    `walk_source` has already dropped build directories, so a `.js` found here is one
    committed into the source tree rather than one a build produced -- though on this
    task a build that produces JavaScript at all is a finding of its own, since
    nothing in a cargo build emits it.

    The list includes `.wasm`: WebAssembly is the compiled form of a language that is
    not Rust, and a `.wasm` in the tree is the only artefact here that could carry a
    complete parser in a form the reviewer cannot read.
    """
    if suffix == MISSING:
        pytest.fail("the contract's forbidden extension list was unreadable")
    hits = [p for p in files if p.suffix.lower() == suffix]
    assert not hits, (
        f"{len(hits)} file(s) with the forbidden extension {suffix}: "
        f"{', '.join(rel(REPO, p) for p in hits[:8])}"
    )


@pytest.mark.parametrize("name", sorted(set(
    n for n in FORBIDDEN_NAMES if n != MISSING
) or {MISSING}))
def test_forbidden_name_absent(name: str, files):
    """One forbidden filename, anywhere in the tree.

    The extension list catches sources; this catches the packaging around them. A
    `package.json` or a `yarn.lock` in a Rust submission describes a JavaScript build
    that is still expected to run, and `node_modules` is a directory of someone
    else's parser.
    """
    if name == MISSING:
        pytest.fail("the contract's forbidden name list was unreadable")
    hits = [p for p in files if p.name == name]
    dirs = [p for p in REPO.rglob(name) if p.is_dir()] if "." not in name else []
    found = [rel(REPO, p) for p in hits] + [rel(REPO, p) + "/" for p in dirs]
    assert not found, (
        f"{name} is still in the submission at {', '.join(found[:6])}. It is part "
        f"of the JavaScript packaging; a Rust build reads none of it."
    )


def test_no_javascript_by_content(files):
    """JavaScript identified by what is in it rather than by its name.

    The extension check is evaded by renaming: `expression.js` copied to
    `docs/expression.txt` is still the implementation, and a submission that loads it
    at run time has not ported anything.  This looks for the two shapes State A's own
    files have -- an ES-module `export` of the names acorn exports, and the
    `#!/usr/bin/env node` shebang -- in files the extension list would let through.

    Scoped to authored files, so a preserved `acorn/CHANGELOG.md` quoting a code
    sample does not produce the same finding on every honest run.
    """
    candidates = [p for p in srbscan.authored(files)
                  if p.suffix.lower() not in srbscan.js_suffixes()
                  and not srbscan.looks_binary(p)]
    hits: list[str] = []
    for path in candidates:
        text = srbscan.read_text(path, 200_000)
        if not text:
            continue
        shebang = text.startswith("#!") and "node" in text.splitlines()[0]
        module = srbscan.first_match(
            text, r"^\s*export\s+(?:default\s+)?(?:function|class|const|let|var)\b",
            flags=re.MULTILINE)
        require = srbscan.first_match(text, r"\brequire\(\s*['\"][./]")
        if shebang or (module and require):
            why = "a node shebang" if shebang else "ES-module exports and require()"
            hits.append(f"{rel(REPO, path)}: {why}")
    assert not hits, (
        "files that are not named like JavaScript but read like it:\n"
        + "\n".join(f"  {h}" for h in hits[:10])
        + "\n\nA renamed source is still a source. Is this documentation, or is it "
          "State A kept under another extension?"
    )


def test_no_compiled_artefacts(files):
    """Compiled output in the source tree, identified by magic where possible.

    The shape this task has to catch is a Rust binary built somewhere else and
    committed, because a Makefile that copies a prebuilt binary into place is a
    rewrite nobody has to have written.  `walk_source` has already dropped `target/`,
    so an ELF found here is one committed beside the sources.

    `.wasm` appears here and in the extension list, deliberately: the extension check
    reports the name and this reports the bytes, and a `.wasm` renamed to `.dat` only
    trips the second.
    """
    suspects: list[tuple[str, str]] = []
    for path in files:
        if srbscan.is_elf(path):
            suspects.append((rel(REPO, path), "\\x7fELF (a compiled binary)"))
        elif srbscan.is_archive(path):
            suspects.append((rel(REPO, path), "!<arch> (a static library)"))
        elif srbscan.is_wasm(path):
            suspects.append((rel(REPO, path), "\\x00asm (a WebAssembly module)"))
        elif path.suffix.lower() in srbscan.BINARY_SUFFIXES:
            suspects.append((rel(REPO, path), f"the suffix {path.suffix}"))
    assert not suspects, (
        "compiled artefacts in the source tree:\n"
        + "\n".join(f"  {p}: {why}" for p, why in suspects[:12])
        + "\n\nThe build produces these. A committed one is code the submission did "
          "not have to write; whose code it is, is the reading."
    )


# --------------------------------------------------------------------------- #
# There should be Rust where it was
# --------------------------------------------------------------------------- #

def test_rust_sources_exist(rust_files):
    """Some Rust, at all.

    The weakest possible form of the question and worth asking first: a submission
    with no `.rs` in it has not been read wrongly by a later check, it has not been
    done.
    """
    assert rust_files, (
        "the submission contains no .rs file. State A is 77 JavaScript files "
        "implementing an ECMAScript parser; State B is a Rust workspace. There is "
        "nothing here for cargo to have built the binaries from."
    )


def test_cargo_workspace_present(files):
    """A workspace manifest, and a manifest per crate the contract names.

    Not a style rule.  The contract's State B is four crates behind a Makefile, and
    stage 2 runs `make build` and then looks for two binaries at fixed paths.  A tree
    with Rust in it but no `Cargo.toml` builds nothing, and a reviewer reading only
    the source would have to guess whether that was the case.
    """
    root_manifest = REPO / "Cargo.toml"
    assert root_manifest.is_file(), (
        f"no Cargo.toml at the root of {REPO}. The contract's build driver is "
        f"'{(srbscan.CONTRACT.get('state_b') or {}).get('build_driver')}' and there "
        f"is no workspace manifest for it to drive."
    )
    manifests = [rel(REPO, p) for p in files if p.name == "Cargo.toml"]
    crates = [c.get("name") for c in
              (srbscan.CONTRACT.get("state_b") or {}).get("crates") or []]
    assert len(manifests) >= 2, (
        f"one Cargo.toml, at the root only. The contract describes {len(crates)} "
        f"crates ({', '.join(str(c) for c in crates)}); a single-manifest tree is "
        f"either a different structure than the contract's or a workspace whose "
        f"members do not exist. Manifests found: {manifests}"
    )


def test_rust_logic_line_floor(rust_files):
    """Enough Rust that it could be an implementation.

    The floor is the contract's, not this file's, and it is a floor rather than a
    target: State A is a hand-written ECMAScript parser, and binaries produced from
    materially less Rust than the floor are a wrapper around something else.  What
    this cannot say is which something else -- that is `no-embedded-reference` and
    `no-interpreter`, and both are questions for the reviewer.
    """
    floor = (srbscan.CONTRACT.get("native_code_policy") or {}).get(
        "min_rust_logic_lines")
    if not isinstance(floor, int):
        pytest.fail("the contract states no min_rust_logic_lines")
    count = srbscan.count_rust_logic_lines(rust_files)
    assert count >= floor, (
        f"{count} lines of Rust logic across {len(rust_files)} file(s), below the "
        f"contract's floor of {floor}. State A is an ECMAScript parser, a tokenizer, "
        f"an error-tolerant parser and an AST walker. A port this much smaller than "
        f"the thing it ports is delegating the work somewhere; where, is the reading."
    )


@pytest.mark.srb_skip_ok
def test_rust_is_the_bulk_of_the_code(files, rust_files):
    """Rust outweighs every other language in the submission.

    Not a rule about counts for their own sake.  The question is whether the port is
    the product or a veneer over one, and a tree where the Rust is a hundred lines
    beside a megabyte of something else answers it.  Generated directories are
    already excluded, so a build's output does not count against a submission.

    The documentation exemption is wide on this task: State A ships three READMEs,
    three CHANGELOGs and a 20 KB SVG logo, all of which the migration keeps.
    """
    if not rust_files:
        pytest.skip("no Rust at all; test_rust_sources_exist reports that")
    rust_bytes = sum(p.stat().st_size for p in rust_files if p.is_file())
    other: dict[str, int] = {}
    for path in files:
        if path.suffix == ".rs" or not path.is_file():
            continue
        suffix = path.suffix.lower() or "(none)"
        other[suffix] = other.get(suffix, 0) + path.stat().st_size
    biggest = sorted(other.items(), key=lambda kv: -kv[1])[:5]
    documentation = {".md", ".txt", "", ".svg", ".html", ".toml", ".lock",
                     ".whitelist", ".unsupported-features", ".editorconfig"}
    competing = [(s, n) for s, n in biggest if s not in documentation]
    for suffix, size in competing:
        assert size <= rust_bytes, (
            f"{suffix} accounts for {size} bytes against {rust_bytes} bytes of .rs. "
            f"What is the {suffix} for, and is the Rust the product or a wrapper "
            f"around it? (largest non-Rust suffixes: "
            f"{', '.join(f'{s}={n}' for s, n in biggest)})"
        )


def test_every_state_a_module_has_a_counterpart(rust_files):
    """Each of State A's implementation source files, looked for by name in Rust.

    A weak check by construction and useful anyway.  Nothing requires a port to keep
    upstream's file layout -- `tokenize.js` may reasonably become part of a larger
    `lexer.rs` -- so a miss here is not a defect and the check does not fail on one.
    What it produces is a *map*: which of State A's twenty-odd implementation modules
    have an obvious counterpart and which do not, so the reviewer opens the Rust for
    the ones that do not rather than reading all of it.

    Reported through the failure message of a check that fails only when *nothing*
    lines up, because a scan that reports a map and passes is a scan whose output
    nobody reads.
    """
    prefixes = srbscan.impl_prefixes()
    stems = sorted({
        srbscan.rel(ORIGINAL, p).rsplit("/", 1)[-1][:-3]
        for p in srbscan.walk_source(ORIGINAL)
        if p.suffix == ".js" and any(
            srbscan.rel(ORIGINAL, p).startswith(pre) for pre in prefixes)
    })
    if not stems:
        pytest.fail(
            f"no State A implementation sources found under {prefixes}; the "
            f"original mount or the contract's anchor list is wrong")
    rust_stems = {p.stem for p in rust_files}
    matched = sorted(s for s in stems if s in rust_stems
                     or s.replace("-", "_") in rust_stems)
    missing = [s for s in stems if s not in matched]
    assert matched, (
        f"none of State A's {len(stems)} implementation modules has a Rust file of "
        f"the same name. That is legal -- a port may restructure freely -- but it "
        f"means this scan can offer the reviewer no map at all. State A's modules: "
        f"{', '.join(stems)}. Rust files: "
        f"{', '.join(sorted(rust_stems)[:20])}"
    )
    if missing:
        print(f"[map] {len(matched)}/{len(stems)} State A modules have a "
              f"same-named Rust file. Without one: {', '.join(missing)}")


# --------------------------------------------------------------------------- #
# What State A left behind
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("relpath", RETAINED)
def test_retained_path_still_there(relpath: str):
    """One of the eight paths the task keeps.

    A missing one is not always cheating -- a reviewer should read `README.md` gone
    as sloppiness and `acorn/LICENSE` gone as something else entirely -- so this
    reports the path and lets the reading happen.
    """
    if relpath == MISSING:
        pytest.fail("the contract's retained path list was unreadable")
    target = REPO / relpath
    assert target.exists(), (
        f"{relpath} is a retained path and it is not in the submission. The "
        f"contract keeps it because it states part of the task's terms: the licence, "
        f"the project's own history, or who wrote it."
    )


@pytest.mark.parametrize("relpath", [
    p for p in RETAINED if p == MISSING or p.endswith("LICENSE")])
def test_license_is_byte_identical(relpath: str):
    """One of the three LICENSE files, hashed against State A's copy.

    The port is a derivative work and the licence travels with it unchanged.  Unlike
    the documents, there is nothing to read here: a LICENSE that differs from
    upstream's by one byte is a changed licence, which is why this is the one
    retention check stated as an objective fact rather than a judgement.
    """
    if relpath == MISSING:
        pytest.fail("the contract's retained path list was unreadable")
    ours, theirs = REPO / relpath, ORIGINAL / relpath
    if not theirs.is_file():
        pytest.fail(f"State A has no {relpath} at {theirs}; the mount is wrong")
    if not ours.is_file():
        pytest.fail(f"the submission has no {relpath}; it is a retained path")
    mine, upstream = srbscan.sha256(ours), srbscan.sha256(theirs)
    assert mine == upstream, (
        f"{relpath} differs from State A's ({mine[:16]} vs {upstream[:16]}, "
        f"{ours.stat().st_size} bytes vs {theirs.stat().st_size}). The port is a "
        f"derivative work and the licence travels with it unchanged."
    )
