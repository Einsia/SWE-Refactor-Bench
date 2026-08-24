"""Is the C gone, and is there Java where it was?

The closure question, in the several forms it takes on this task.  Stage 2 builds
a jar and compares 4,062 behavioural cases against the C reference; none of those
cases can tell you that `deflate.c` is still sitting in the tree beside the port,
because a repository that kept every C source *and* wrote a complete Java
implementation builds a perfectly good jar.  The artifact cannot testify about
what was left behind.

Every list here is derived: the eighteen translation units and the nine private
headers come from source-contract.json, the preserved paths come from the same
place, and the file inventory comes from walking `/opt/original`.  A list written
in this file would be a second description of the upstream release, free to drift
from the first, and the failure mode of that drift is a check that quietly stops
looking for something.

Nothing here is a verdict.  Every failure is addressed to the reviewer as a place
to look: this module cannot tell a vendored zlib from an honest port, and it does
not try to.
"""

from __future__ import annotations

import pytest

import srbscan
from srbscan import ORIGINAL, REPO, rel

pytestmark = pytest.mark.scan

MISSING = "<state-a-unreadable>"


def _contract_list(*keys: str) -> list[str]:
    """A list from the contract, or the sentinel if the contract is unreadable.

    The sentinel rather than an empty list: an empty `parametrize` is a module
    that silently contributes nothing, and "the contract could not be read" is
    itself the most important thing this module could report.
    """
    node = srbscan.CONTRACT
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return [MISSING]
        node = node[key]
    if isinstance(node, list) and node:
        return [str(item) for item in node]
    return [MISSING]


C_SOURCES = _contract_list("forbidden_paths", "c_sources_that_must_be_removed")
PRIVATE_HEADERS = _contract_list("forbidden_paths",
                                 "internal_headers_that_must_be_removed")
PRESERVED = _contract_list("preserved_paths", "paths")
FORBIDDEN_EXTENSIONS = _contract_list("forbidden_paths", "extensions")


def test_contract_is_readable():
    """The contract loaded at all.

    First, because every other check in this module derives its list from it and
    a check parametrized over the sentinel passes vacuously.
    """
    assert srbscan.CONTRACT, (
        f"source-contract.json did not load from {srbscan.CONTRACT_PATH}; every "
        f"derived list in this module is a single placeholder, so the module's "
        f"silence means nothing"
    )
    assert srbscan.CONTRACT.get("task") == "lang02-zlib-c-to-java", (
        f"the contract in the image describes "
        f"{srbscan.CONTRACT.get('task')!r}, not this task"
    )


def test_both_trees_are_mounted():
    """Both mounts resolved to something that looks like zlib.

    A scan pointed one level off the repository root walks a directory holding
    one entry, derives an empty list of C sources and reports a clean tree.  This
    is the check that says the mount is wrong, so that no other check has to.
    """
    for label, root in (("original", ORIGINAL), ("workspace", REPO)):
        assert root.is_dir(), f"{label} is not a directory at {root}"
    assert any((ORIGINAL / m).exists() for m in srbscan.ROOT_MARKERS), (
        f"{ORIGINAL} holds none of {srbscan.ROOT_MARKERS}, so it is not the "
        f"State A tree and nothing derived from it is trustworthy"
    )
    # Deliberately not asserted for the workspace.  A submission that moved the
    # repository root is a finding for the reviewer, not a broken scan, and the
    # next check is the one that reports it.
    if not any((REPO / m).exists() for m in srbscan.ROOT_MARKERS):
        pytest.fail(
            f"{REPO} holds none of {srbscan.ROOT_MARKERS}: no CMakeLists.txt, no "
            f"LICENSE and no zlib.h at the root the grader mounts. All three are "
            f"preserved paths, so either they were deleted or the tree was moved "
            f"under a subdirectory. Top-level entries: "
            f"{sorted(p.name for p in REPO.iterdir())[:20]}"
        )


# --------------------------------------------------------------------------- #
# The C should be gone
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("relpath", C_SOURCES)
def test_c_source_is_gone(relpath: str):
    """One of the eighteen translation units, anywhere in the tree.

    Anywhere, not just at its State A path: moving `deflate.c` to `legacy/` or
    `attic/` does not port it.  The search is by basename because that is what a
    move preserves, and the finding names every hit so the reviewer can tell a
    forgotten file from a directory of them.
    """
    if relpath == MISSING:
        pytest.fail("the contract's C source list was unreadable")
    name = relpath.rsplit("/", 1)[-1]
    hits = [p for p in srbscan.walk_source(REPO) if p.name == name]
    assert not hits, (
        f"{name} is still in the submission at "
        f"{', '.join(rel(REPO, p) for p in hits[:5])}. State A had it at "
        f"{relpath}; the task deletes it. Is this a file the port forgot to "
        f"remove, or is the C still the implementation?"
    )


@pytest.mark.parametrize("name", PRIVATE_HEADERS)
def test_private_header_is_gone(name: str):
    """One of the nine internal headers.

    Separate from the translation units because the reviewer reads them
    differently: a leftover `.c` is dead weight, and a leftover `deflate.h` beside
    Java that mentions `deflate_state` suggests the port was done by transcribing
    the struct rather than the algorithm. Either is legal. Which one it is, is a
    reading.
    """
    if name == MISSING:
        pytest.fail("the contract's private header list was unreadable")
    hits = [p for p in srbscan.walk_source(REPO) if p.name == name]
    assert not hits, (
        f"{name} is still in the submission at "
        f"{', '.join(rel(REPO, p) for p in hits[:5])}. It is a private header of "
        f"the C implementation and nothing in a Java build reads it."
    )


def test_no_unallowlisted_headers(files):
    """A `.h` that is neither `zlib.h` nor licensed as removable.

    zlib.h stays: it is the behavioural specification and the port owes the same
    promises. The nine private headers must go, and each has its own check.

    `zconf.h` is neither, and it is not reported. It sits in the contract's
    `removable_paths` -- "removing these is expected" -- and in the brief's "May
    go" list, so keeping it is licensed; the required `no-c-implementation` gate
    tells the reviewer in as many words that `zconf.h` "is supposed to still be
    there and [is] not a finding", because `zlib.h` includes it. A finding here
    would contradict the gate that consumes it, on a submission that had done
    nothing wrong.

    Licence is not the same as allowlist, and they are read from different keys on
    purpose: stage 2 fails a submission for a *missing* allowlisted header, so
    allowlisting `zconf.h` would make deleting it -- the other half of the same
    licence -- a graded failure.

    The suffix set is `.h/.hpp/.hh/.hxx`, so `zconf.h.in` and `zconf.h.cmakein`
    are not reachable from here whatever the allowlist says. They are licensed
    too, and `test_forbidden_extension_absent` owns unexpected suffixes.
    """
    allow = srbscan.HEADER_ALLOWLIST
    licensed = srbscan.REMOVABLE_PATHS
    stray = [p for p in files
             if p.suffix in (".h", ".hpp", ".hh", ".hxx")
             and rel(REPO, p) not in allow and p.name not in allow
             and rel(REPO, p) not in licensed and p.name not in licensed]
    assert not stray, (
        f"{len(stray)} header(s) beyond {sorted(allow)} and the removable list: "
        f"{', '.join(rel(REPO, p) for p in stray[:10])}"
    )


@pytest.mark.parametrize("suffix", sorted(set(
    s for s in FORBIDDEN_EXTENSIONS if s != MISSING
) or {MISSING}))
def test_forbidden_extension_absent(suffix: str, files):
    """One forbidden extension, over the submitted tree.

    `walk_source` has already dropped build directories, so a `.class` found here
    is one committed into the source tree rather than one a build produced. That
    distinction is the whole reason this is a scan finding and not a build check:
    the same bytes are unremarkable under `build-shared/` and are a rewrite
    nobody had to write when they are under `src/`.
    """
    if suffix == MISSING:
        pytest.fail("the contract's forbidden extension list was unreadable")
    hits = [p for p in files if p.suffix == suffix]
    assert not hits, (
        f"{len(hits)} file(s) with the forbidden extension {suffix}: "
        f"{', '.join(rel(REPO, p) for p in hits[:8])}"
    )


def test_no_bytecode_by_magic(files):
    """Bytecode identified by its first four bytes rather than by its name.

    The suffix check above is evaded by renaming. `0xCAFEBABE` is not: a class
    file is loadable whatever it is called, and a jar is a zip whatever it is
    called. Both are reported with the magic quoted, because "this file starts
    with PK" is a fact the reviewer can re-check in one command.
    """
    suspects = []
    for path in files:
        if srbscan.is_class_file(path):
            suspects.append((rel(REPO, path), "cafebabe (a JVM class file)"))
        elif srbscan.is_zip(path) and path.suffix.lower() not in (
                ".zip", ".odt", ".docx", ".xlsx"):
            suspects.append((rel(REPO, path), "PK.. (a zip container, e.g. a jar)"))
        elif srbscan.is_elf(path):
            suspects.append((rel(REPO, path), "\\x7fELF (a compiled object)"))
        elif srbscan.is_archive(path):
            suspects.append((rel(REPO, path), "!<arch> (a static library)"))
    assert not suspects, (
        "compiled artefacts in the source tree, by magic rather than by name:\n"
        + "\n".join(f"  {p}: starts with {why}" for p, why in suspects[:12])
    )


# --------------------------------------------------------------------------- #
# There should be Java where it was
# --------------------------------------------------------------------------- #

def test_java_sources_exist(java_files):
    """Some Java, at all.

    The weakest possible form of the question and worth asking first: a
    submission with no `.java` in it has not been read wrongly by a later check,
    it has not been done.
    """
    assert java_files, (
        "the submission contains no .java file. State A is 18 C translation "
        "units; State B is a Java library. There is nothing here to have "
        "compiled the jar from."
    )


def test_java_logic_line_floor(java_files):
    """Enough Java that it could be an implementation.

    The floor is the contract's, not this file's, and it is a floor rather than a
    target: State A is 13,192 hand-written lines of C, and a jar produced from
    materially less Java than the floor is a wrapper around something else. What
    it cannot say is which something else -- that is `no-jdk-deflate` and
    `no-vendored-zlib`, and both are questions for the reviewer.
    """
    floor = srbscan.CONTRACT.get("jvm_code_policy", {}).get(
        "min_java_logic_lines")
    if not isinstance(floor, int):
        pytest.fail("the contract states no min_java_logic_lines")
    count = srbscan.count_java_logic_lines(java_files)
    assert count >= floor, (
        f"{count} lines of Java logic across {len(java_files)} file(s), below "
        f"the contract's floor of {floor}. State A is "
        f"{srbscan.CONTRACT.get('source_tree_facts', {}).get('hand_written_c_lines')} "
        f"hand-written lines of C. A port this much smaller than the thing it "
        f"ports is delegating the work somewhere; where, is the reading."
    )


@pytest.mark.srb_skip_ok
def test_java_is_the_bulk_of_the_tree(files, java_files):
    """Java outweighs every other language in the submission.

    Not a rule about counts for their own sake. `java-is-primary` asks whether the
    port is the product or a veneer over one, and a tree where the Java is a
    hundred lines beside a megabyte of something else answers it. Generated
    directories are already excluded, so a build's output does not count against
    a submission here.

    Licensed to skip, because a tree with no Java has nothing to weigh and
    `test_java_sources_exist` is the check that reports the absence. Without the
    marker the plugin scores an unlicensed skip as a miss, which put a second
    flagged lead in front of the reviewer that leads to the same place as the
    first -- and the reviewer is told every flagged line is somewhere to look.
    """
    if not java_files:
        pytest.skip("no Java at all; test_java_sources_exist reports that")
    java_bytes = sum(p.stat().st_size for p in java_files if p.is_file())
    other = {}
    for path in files:
        if path.suffix == ".java" or not path.is_file():
            continue
        suffix = path.suffix.lower() or "(none)"
        other[suffix] = other.get(suffix, 0) + path.stat().st_size
    biggest = sorted(other.items(), key=lambda kv: -kv[1])[:5]
    # Text formats that legitimately dwarf the code: the three RFCs alone are
    # ~180 KB of preserved specification, and ChangeLog is another 80 KB.
    documentation = {".txt", ".md", "", ".pdf", ".3", ".map", ".in", ".cmakein"}
    competing = [(s, n) for s, n in biggest if s not in documentation]
    for suffix, size in competing:
        assert size <= java_bytes, (
            f"{suffix} accounts for {size} bytes against {java_bytes} bytes of "
            f".java. What is the {suffix} for, and is the Java the product or a "
            f"wrapper around it? (largest non-Java suffixes: "
            f"{', '.join(f'{s}={n}' for s, n in biggest)})"
        )


# --------------------------------------------------------------------------- #
# What State A left behind
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("relpath", PRESERVED)
def test_preserved_path_still_there(relpath: str):
    """One of the eight paths the task keeps.

    A missing one is not always cheating -- a reviewer should read `README` gone
    as sloppiness and `LICENSE` gone as something else entirely -- so this reports
    the path and lets the reading happen.

    No gate hangs off it, and none can usefully: an advisory gate sits outside the
    conjunction grade_audit builds its verdict from, so its answer is read by
    nothing, and a required one would void a submission for a missing README. The
    finding goes into the scan digest the review reads, which is the whole of what
    a gate here could accomplish.
    """
    if relpath == MISSING:
        pytest.fail("the contract's preserved path list was unreadable")
    target = REPO / relpath
    assert target.exists(), (
        f"{relpath} is a preserved path and it is not in the submission. The "
        f"contract keeps it because it states part of the task's contract: the "
        f"licence, the API, the wire format, or the project's own history."
    )


def test_license_is_byte_identical():
    """LICENSE, hashed against State A's copy.

    The port is a derivative work and the licence travels with it unchanged. A
    difference here is worth reading rather than failing on: a reformatted copy
    and a relicensed one are the same size of diff and nothing like the same
    finding.
    """
    ours, theirs = REPO / "LICENSE", ORIGINAL / "LICENSE"
    if not theirs.is_file():
        pytest.fail(f"State A has no LICENSE at {theirs}; the mount is wrong")
    if not ours.is_file():
        pytest.fail("the submission has no LICENSE")
    mine, upstream = srbscan.sha256(ours), srbscan.sha256(theirs)
    assert mine == upstream, (
        f"LICENSE differs from State A's ({mine[:16]} vs {upstream[:16]}, "
        f"{ours.stat().st_size} bytes vs {theirs.stat().st_size}). What changed, "
        f"and does the change alter the terms?"
    )


def test_zlib_h_is_byte_identical():
    """zlib.h, hashed against State A's copy.

    The header is preserved as the behavioural specification, so an edit to it is
    an edit to the thing the port is measured against. A submission that widened
    a prototype in the spec to match what it implemented has moved the goalposts,
    and stage 2's `api-shape-unchanged` reads the contract rather than this file,
    so this is the only place the edit shows up.
    """
    ours, theirs = REPO / "zlib.h", ORIGINAL / "zlib.h"
    if not theirs.is_file():
        pytest.fail(f"State A has no zlib.h at {theirs}; the mount is wrong")
    if not ours.is_file():
        pytest.fail("the submission has no zlib.h; it is a preserved path")
    mine, upstream = srbscan.sha256(ours), srbscan.sha256(theirs)
    assert mine == upstream, (
        f"zlib.h differs from State A's ({mine[:16]} vs {upstream[:16]}). It is "
        f"the specification the port is graded against. Which declarations "
        f"changed, and do they match what the Java actually provides?"
    )
