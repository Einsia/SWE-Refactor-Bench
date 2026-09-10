"""Did the C leave the tree, and is there Rust where it was?

Every check here reads files.  None of them decides anything: `swerefactor.scan`
records each with `required = False`, and the gate is the eight prose questions in
evaluation.toml, answered by a model with both trees open.  What these produce is
the part a string can settle -- *which* of State A's twenty translation units is
still on disk, *which* internal header survived -- so the reviewer spends its turns
on the part a string cannot: whether the Rust is an implementation or a shell, and
whether it is a port or a transliteration.

The file names come from State A itself rather than from a list written here.  A
list would be a second description of the upstream release, free to drift from the
first; `original/src/*.c` is the release.
"""

from __future__ import annotations

import re

import pytest
import srbscan
from srbscan import ORIGINAL, REPO

pytestmark = pytest.mark.scan


def _names(pattern: str) -> list[str]:
    src = ORIGINAL / "src"
    if not src.is_dir():
        return []
    return sorted(p.name for p in src.glob(pattern))


#: State A's twenty C translation units, and its thirteen headers.  `cmark.h` is
#: filtered out of the header list: it is the installed ABI contract and has its
#: own check in the other direction.
C_UNITS = _names("*.c")
PRIVATE_HEADERS = [n for n in _names("*.h") if f"src/{n}" not in srbscan.HEADER_ALLOWLIST]
#: The generated tables and the re2c input.  Data, not implementation -- but the
#: `.c` the scanner generates is implementation, and `entities.inc` being present
#: with no Rust that reads it is worth a look.
GENERATED_C = _names("*.inc") + _names("*.re")

#: Directories whose purpose disappears with the C, from source-contract.json's
#: `removable_paths`.  Their presence is not a defect; C *inside* them is not the
#: library either.  Both facts are worth stating so the reviewer does not have to
#: rediscover them.
REMOVABLE = ("api_test", "fuzz")


# --------------------------------------------------------- the C, unit by unit

@pytest.mark.parametrize("name", C_UNITS or [srbscan.UNREADABLE])
def test_c_translation_unit_is_gone(name):
    """One check per translation unit State A shipped, by name.

    Per-unit rather than "no .c files exist" because the answer is a list and the
    reviewer needs the list: three of these gone and seventeen present is a
    different submission from seventeen gone and three present, and the second one
    is usually a migration that stalled rather than a cheat.
    """
    srbscan.require_state_a(name)
    hits = [srbscan.rel(REPO, p) for p in srbscan.walk_source(REPO)
            if p.name == name]
    assert not hits, (
        "src/%s is still in the tree at %s. That is where State A's %s lived; "
        "whether it is still the implementation depends on what compiles it."
        % (name, ", ".join(sorted(hits)[:4]), name))


@pytest.mark.parametrize("name", PRIVATE_HEADERS or [srbscan.UNREADABLE])
def test_private_header_is_gone(name):
    """The internal headers described the C's data layout.

    A surviving `node.h` is the interesting case: it usually means the Rust is
    mirroring the C's structs by hand rather than owning its own representation,
    which is a judgement for the reviewer and a fact worth handing over.
    """
    srbscan.require_state_a(name)
    hits = [srbscan.rel(REPO, p) for p in srbscan.walk_source(REPO)
            if p.name == name]
    assert not hits, (
        "src/%s is still in the tree at %s; it was internal to the C "
        "implementation and describes its layout"
        % (name, ", ".join(sorted(hits)[:4])))


@pytest.mark.parametrize("name", GENERATED_C or [srbscan.UNREADABLE])
def test_generated_c_table_is_gone(name):
    """`case_fold.inc`, `entities.inc`, `scanners.re`.

    Reported, not condemned. The *data* in these is expected to survive in some
    form -- the entity table is the HTML5 entity table however it is spelled -- so
    a hit here is a question about what reads the file, which is the reviewer's.
    """
    srbscan.require_state_a(name)
    hits = [srbscan.rel(REPO, p) for p in srbscan.walk_source(REPO)
            if p.name == name]
    assert not hits, (
        "src/%s survives at %s. The table's contents are data and may legitimately "
        "be carried across; this file is the C's form of it, so check what reads it."
        % (name, ", ".join(sorted(hits)[:4])))


# ------------------------------------------------- C anywhere, by extension

@pytest.mark.parametrize("suffix", sorted(set(srbscan.C_SOURCE_SUFFIXES)))
def test_no_c_family_source_with_suffix(suffix, files):
    """One check per C-family extension, over the whole tree.

    The by-name checks above cover State A's own files. This covers C that
    arrived: a `port.c` nobody in State A had, a `.cpp` under a vendored path, a
    `.s` next to the Rust. `removable_paths` is annotated rather than excluded --
    C under `api_test/` is not the library, and saying so here is cheaper than the
    reviewer working it out.
    """
    hits = sorted(srbscan.rel(REPO, p) for p in files
                  if p.suffix.lower() == suffix)
    if not hits:
        return
    note = ""
    if all(h.split("/")[0] in REMOVABLE for h in hits):
        note = (" All of these are under %s, which source-contract.json lists as "
                "removable: C there is a test harness or a fuzz target, not the "
                "library." % " or ".join(REMOVABLE))
    assert False, ("%d file(s) with suffix %s: %s.%s"
                   % (len(hits), suffix, ", ".join(hits[:12]), note))


def test_public_header_is_present():
    """`src/cmark.h` is the contract, and its absence is not a migration."""
    assert (REPO / "src" / "cmark.h").is_file(), (
        "src/cmark.h is missing. It is the installed ABI contract every "
        "downstream C consumer compiles against, and the one header State A "
        "shipped that is required to survive.")


@pytest.mark.srb_skip_ok      # absence is test_public_header_is_present's finding
def test_public_header_declares_the_same_surface():
    """Every `cmark_*` identifier State A's header declares still appears in it.

    Textual on purpose, and reported as a lead: this is a set difference over
    identifiers, so a header that was reformatted, recommented or reordered
    produces no finding, while one that dropped an enum value or renamed a
    function produces the exact name. Whether an *addition* is a defect is stage
    2's `no-abi-widening`, measured against the installed header rather than read
    out of the tree.
    """
    ref = ORIGINAL / "src" / "cmark.h"
    got = REPO / "src" / "cmark.h"
    if not ref.is_file():
        # State A always ships src/cmark.h -- it is the ABI this whole task is about.
        # Missing means the frozen tree did not mount, which is a harness fault, and
        # skipping it charged the submission for it: an unlicensed skip is scored as
        # a miss and reaches the reviewer as a flagged finding.
        pytest.fail(
            "SCAN IS BLIND, NOT A SUBMISSION DEFECT: State A's src/cmark.h is not "
            "readable, so the reference ABI is unavailable and this comparison could "
            "not run. Nothing here is a claim about the submission.",
            pytrace=False)
    if not got.is_file():
        pytest.skip("no src/cmark.h in the submission; the check above says so")
    pattern = re.compile(r"\bcmark_[A-Za-z0-9_]*|\bCMARK_[A-Za-z0-9_]*")
    want = set(pattern.findall(srbscan.read_text(ref)))
    have = set(pattern.findall(srbscan.read_text(got)))
    missing = sorted(want - have)
    assert not missing, (
        "%d identifier(s) State A's cmark.h declared no longer appear in the "
        "submission's: %s. A compiled consumer has these baked in."
        % (len(missing), ", ".join(missing[:20])))


# ------------------------------------------------------------- and the Rust

def test_rust_source_is_present(rust_files):
    assert rust_files, (
        "there is not one .rs file in the tree, so whatever produces the "
        "artifacts is not Rust")


@pytest.mark.srb_skip_ok      # no Rust is test_rust_source_is_present's finding
def test_rust_logic_line_floor(rust_files):
    """3,000 lines is the floor source-contract.json publishes.

    A count is a weak instrument and it is here as an orientation number rather
    than as a test: the reviewer is told how much Rust there is before it starts
    reading, and `rust-present` asks whether that Rust is an implementation. A
    submission over the floor can still be filler and a submission under it cannot
    be a port of 22,000 lines of C.
    """
    if not rust_files:
        pytest.skip("no Rust to count; the check above says so")
    lines = srbscan.count_rust_logic_lines(rust_files)
    assert lines >= 3000, (
        "%d lines of Rust logic across %d file(s), below the 3000-line floor "
        "source-contract.json publishes. State A is ~22,000 lines of C."
        % (lines, len(rust_files)))


@pytest.mark.srb_skip_ok      # no Rust is test_rust_source_is_present's finding
def test_every_rust_file_is_reachable_from_a_module_declaration(rust_files):
    """A `.rs` no `mod` names is dead weight, and dead weight inflates a count.

    Approximate by design, and that is why it is advisory: `mod` can be generated
    by a macro, and `include!` can pull a file in without naming it. A hit means
    "this file may not be compiled at all", which is exactly the shape of a
    directory padded out to pass a size check -- and the reviewer can settle it by
    reading the crate root.

    Both escape hatches are narrow enough to get wrong. The `include!` capture has
    to end at the closing quote, not at the `)`: in `include!("punct_table.rs")`
    the character before the paren is a quote, so a pattern anchored on `)` misses
    every legal include and matches only bare `include!(punct_table.rs)`, which is
    not Rust.

    Cargo's target roots are the other one. `lib`/`main`/`mod`/`build` need no
    declaration by convention, but `src/bin/*.rs` needs none either, and a
    `[[bin]]`, `[[example]]` or `[[bench]]` stanza may point `path` anywhere. Both
    are read below, so a binary target does not read as an orphan for being a
    binary target.
    """
    if not rust_files:
        pytest.skip("no Rust to check")
    declared: set[str] = set()
    for path in rust_files:
        text = srbscan.read_text(path)
        for match in re.finditer(r"^\s*(?:pub\s+(?:\([^)]*\)\s*)?)?mod\s+"
                                 r"([A-Za-z_][A-Za-z0-9_]*)\s*;", text,
                                 re.MULTILINE):
            declared.add(match.group(1))
        # Quoted or not, and tolerant of the concat!(env!("OUT_DIR"), "/x.rs")
        # form: take every path-shaped token inside the parentheses rather than
        # requiring one to sit flush against the closing paren.
        for call in re.finditer(r"include(?:_str|_bytes)?!\s*\((.*?)\)\s*;",
                                text, re.S):
            for token in re.finditer(r"""["']([^"']*\.rs)["']""", call.group(1)):
                declared.add(token.group(1).split("/")[-1].removesuffix(".rs"))
    #: Roots Cargo compiles with no `mod` anywhere: the crate roots, plus a
    #: `path` any target stanza names explicitly.
    roots = {"lib", "main", "mod", "build"}
    for manifest in REPO.rglob("Cargo.toml"):
        for match in re.finditer(r"""^\s*path\s*=\s*["']([^"']+\.rs)["']""",
                                 srbscan.read_text(manifest), re.MULTILINE):
            roots.add(match.group(1).split("/")[-1].removesuffix(".rs"))
    orphans = sorted(
        srbscan.rel(REPO, p) for p in rust_files
        if p.stem not in declared and p.stem not in roots
        and p.parent.name not in declared
        # src/bin/*.rs, examples/, benches/, tests/: auto-discovered targets.
        and p.parent.name not in ("bin", "examples", "benches", "tests"))
    assert not orphans, (
        "%d Rust file(s) are named by no `mod` declaration, no `include!`, and no "
        "Cargo target, so they may not be part of any crate: %s. A "
        "macro-generated `mod` would produce the same report, so confirm before "
        "concluding."
        % (len(orphans), ", ".join(orphans[:12])))
