"""Did the Go leave, and is there Zig where it was?

Every list here is derived from ``/opt/original`` rather than typed into this
file.  A literal list of State A's Go sources would be a claim about State A
maintained in two places, and the copy in the scan is the one nobody would notice
going stale -- a renamed source upstream would leave this module checking for a
file that no longer exists and reporting a clean tree.

Deriving it also keeps three different counts from being confused for each other.
State A ships twenty-four ``.go`` files in two populations: the library's thirteen
sources and six ``_test.go`` at the module root, and the NDJSON probe adapter's four
sources and one ``_test.go`` under ``probe/`` and ``cmd/yaml-probe/``.  The contract
grades *twelve* -- all from the library, and ``sorter.go`` is excluded because it is
only reachable from typed ``Marshal``, which is not graded -- and 7,609 is the
logic-line sum over those twelve.  The checks below run over all twenty-four,
because ``no-go-sources`` is about what is on disk rather than about what is graded:
a Go test file left in a Zig repository is the same finding as a Go source, and so
is a kept adapter.  The 7,609 figure appears once, in the Zig floor's message, where
it is the right comparison.

The per-file derivation walks the whole mount, not its root.  A root-only glob is
the shape this module would fail in silently -- it would still be nineteen names
long, still pass, and still report a clean tree with ``probe/probe.go`` sitting in
the submission.  ``test_no_go_anywhere`` would catch that as a class; what would be
lost is the report naming which of State A's files came back.

Nothing in this module is scored.  ``swerefactor.scan`` records every check with
``required = False``, and ``scoring.grade_audit`` gates on the prose gates in
``evaluation.toml``.  What these produce is a place for the reviewer to look.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import srbscan

pytestmark = pytest.mark.scan

# The Go files State A ships, taken from the mount.  `rglob`, not `glob`: five of
# the twenty-four live under probe/ and cmd/yaml-probe/, and a root-only glob would
# leave them unnamed by the per-file check below.  Basenames, because the check
# compares basenames -- a submission that kept a file by moving it should still be
# named -- and `set` because that makes two populations able to share a name without
# producing a duplicate parametrize id.  Sorted so the ids are stable between runs;
# the `or [srbscan.UNREADABLE]` on the parametrize below keeps collection working
# when /opt/original is empty, which is how the build-time collect-check runs.
#
# `is_dir()` and not `any(iterdir())` on purpose: an existing-but-empty mount and a
# missing one both land on the empty list, and both are handled the same way -- by
# the sentinel, at check time, rather than here.  Docker materialises a missing bind
# source as an empty directory, so the two cases are not distinguishable here anyway.
ORIGINAL_GO = sorted(
    {p.name for p in srbscan.ORIGINAL.rglob("*.go")}
) if srbscan.ORIGINAL.is_dir() else []


def _found(paths: list[Path], suffix: str) -> list[Path]:
    return [p for p in paths if p.suffix == suffix]


# --------------------------------------------------------------------------- #
# The Go, per file, by the name State A gave it
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", ORIGINAL_GO or [srbscan.UNREADABLE])
def test_original_go_file_is_gone(name: str, repo: Path, files: list[Path]) -> None:
    """Each of State A's Go files, absent from the submission by that name.

    The sentinel branch is a harness fault, not a pass: this check's whole
    population comes out of State A, so an unreadable mount leaves it with nothing
    to look for.  A skip is neutral by contract and therefore indistinguishable
    from a tree that retired the Go properly.
    """
    srbscan.require_state_a(name)
    hits = [srbscan.rel(repo, p) for p in files if p.name == name]
    assert not hits, (
        f"State A's {name} is still in the submission at: {', '.join(hits)}. "
        f"The Go is State A's implementation; a submission that still carries it "
        f"has either not finished migrating or kept it for reference. Which of "
        f"those it is depends on whether anything builds it."
    )


# No `srb_skip_ok`: this check no longer has a branch that skips.  The marker
# licensed exactly one -- the unmounted-State-A case, now a loud failure -- and
# leaving it would license the next skip somebody adds here without a decision.


def test_no_go_anywhere(repo: Path, files: list[Path]) -> None:
    """Any .go file at all, under any name, at any depth."""
    hits = sorted(srbscan.rel(repo, p) for p in _found(files, ".go"))
    assert not hits, (
        f"{len(hits)} .go file(s) under the submission root: "
        f"{', '.join(hits[:12])}"
        + (f" ... and {len(hits) - 12} more" if len(hits) > 12 else "")
        + ". source-contract.json forbids the .go extension anywhere at any "
          "depth, so this is a contract violation whatever the file contains -- "
          "but read it before deciding whether it is also an unfinished "
          "migration."
    )


# --------------------------------------------------------------------------- #
# The forbidden extensions and names, from the contract, one check each
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("suffix", srbscan.GO_SOURCE_SUFFIXES
                         + srbscan.C_SOURCE_SUFFIXES
                         + srbscan.BINARY_SUFFIXES)
def test_forbidden_suffix_absent(suffix: str, repo: Path,
                                 files: list[Path]) -> None:
    """One check per forbidden extension, so the report names which one."""
    hits = sorted(srbscan.rel(repo, p) for p in files if p.suffix == suffix)
    assert not hits, (
        f"{len(hits)} file(s) with the forbidden {suffix} extension: "
        f"{', '.join(hits[:8])}. The contract allows Zig source and the Zig "
        f"standard library; C, Go and compiled artefacts are all ways of getting "
        f"an implementation the pinned toolchain did not produce."
    )


@pytest.mark.parametrize("name", srbscan.FORBIDDEN_NAMES)
def test_forbidden_name_absent(name: str, repo: Path) -> None:
    """Go module metadata and a vendor directory, at any depth."""
    hits = sorted(srbscan.rel(repo, p)
                  for p in srbscan.REPO.rglob(name)
                  if not any(part in srbscan.GENERATED_DIR_NAMES
                             for part in p.relative_to(srbscan.REPO).parts[:-1]))
    assert not hits, (
        f"{name} is present at: {', '.join(hits)}. The contract names it as "
        f"forbidden: it is Go module metadata or a vendoring directory, and "
        f"neither has a role in a Zig build that uses only the standard library."
    )


# --------------------------------------------------------------------------- #
# Is there Zig where the Go was?
# --------------------------------------------------------------------------- #

def test_build_script_present(repo: Path) -> None:
    assert (repo / "build.zig").is_file(), (
        "there is no build.zig at the submission root. State A ships one, so "
        "this is a deletion rather than an omission."
    )


def test_build_manifest_present(repo: Path) -> None:
    assert (repo / "build.zig.zon").is_file(), (
        "there is no build.zig.zon at the submission root. State A ships one, "
        "and `zig build` reads it for the package name and fingerprint."
    )


def test_zig_sources_exist(zig_files: list[Path]) -> None:
    assert zig_files, (
        "there are no .zig files under the submission root at all. Whatever else "
        "happened here, no Zig implementation was written."
    )


def test_zig_logic_line_floor(repo: Path, zig_files: list[Path]) -> None:
    """The contract's 4,000-line floor, counted the way stage 2 counts it.

    A rejection threshold for a shim, not a definition of done: State A is 7,609
    logic lines of Go, and a port that clears this by a hundred lines is not
    thereby a port. The reviewer is told as much -- what this check is for is the
    submission that is two orders of magnitude under it, where no reading is
    required to know something is missing.
    """
    counted = {
        srbscan.rel(repo, p): srbscan.zig_logic_lines(srbscan.read_text(p))
        for p in zig_files
    }
    total = sum(counted.values())
    biggest = sorted(counted.items(), key=lambda kv: -kv[1])[:6]
    assert total >= 4000, (
        f"{total} non-blank non-comment lines of Zig across {len(zig_files)} "
        f"file(s), against a contract floor of 4,000 and State A's 7,609 lines of "
        f"Go. Largest: "
        + ", ".join(f"{name} ({n})" for name, n in biggest)
        + ". Under the floor is not by itself proof of a shim -- read what is "
          "there and say what is missing."
    )


def test_every_zig_file_is_imported(repo: Path, zig_files: list[Path]) -> None:
    """A .zig no other .zig and no build.zig names is dead weight.

    Not a violation of anything: a submission may keep a scratch file. It is
    reported because padding a directory to clear a line count is one of the ways
    a shim is dressed up as an implementation, and an unreferenced 3,000-line file
    is what that looks like from here.
    """
    if not zig_files:
        pytest.skip("no Zig to check reachability for")
    corpus = "\n".join(srbscan.read_text(p) for p in zig_files)
    build_text = srbscan.read_text(repo / "build.zig")
    roots = {"build.zig", "src/main.zig", "main.zig", "build.zig.zon"}
    orphans = []
    for path in zig_files:
        name = srbscan.rel(repo, path)
        if name in roots:
            continue
        stem = Path(name).name
        if stem in corpus or stem in build_text:
            continue
        orphans.append(f"{name} ({srbscan.zig_logic_lines(srbscan.read_text(path))} lines)")
    assert not orphans, (
        f"{len(orphans)} .zig file(s) whose name appears in no other Zig source "
        f"and not in build.zig: {', '.join(sorted(orphans)[:10])}. A file nothing "
        f"imports cannot be part of the implementation. Check whether it is a "
        f"leftover or whether it is padding."
    )


test_every_zig_file_is_imported = pytest.mark.srb_skip_ok(
    test_every_zig_file_is_imported)
