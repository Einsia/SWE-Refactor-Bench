#!/usr/bin/env python3
"""Not one byte of C, C++ or assembly source has changed.

Contract §3. Every `.c`, `.h` and `.S` file State A ships under src/ and test/ is
hashed and compared -- 352 of them, one check each.

Against the reference tree, not against a manifest
--------------------------------------------------
Stage 2 grades this from a frozen `data/sources.json`, and has to: by the time it
runs it holds a matrix of build trees and no State A to compare them with. This stage has
State A mounted at `/opt/original`, so the manifest is computed from it at run
time.

That is one fewer duplicated input -- a Docker build context cannot reach outside
itself, so a frozen copy here would be a second file claiming to be State A's
checksums, and `_shared_input_drift` only notices duplicates of things in
`environment/`. It is also a stronger claim. A manifest asserts what State A used
to contain; hashing the mounted tree compares against what State A *is*, and it is
the same tree the reviewer is reading in the next turn.

On this being advisory
----------------------
A sha256 mismatch is not a matter of opinion, so it is fair to ask why these checks
cannot fail the stage by themselves.

The scan's contract is that nothing it reports gates: `swerefactor.scan` sets
`required = False` on every check it emits, and `scoring.grade_audit` gates on
the required checks, which are the six prose gates. That is not a concession, it is
what makes the scan safe to add checks to -- a suite where some mechanical
observations gate and some do not is one where the next person to add a regex has to
guess which kind theirs is.

And the gate these feed, `sources_untouched`, is required, and its prompt now says:
if the scan reports a mismatch, open the file, confirm it differs, fail the gate. A
mismatch survives as a *fail* rather than as a deduction, and it arrives with the
path. What the reviewer adds is the half a hash cannot see -- a new file compiled
in, a header shadowed earlier on the include path, a source replaced by a symlink, a
file left byte-identical and then excluded from the build.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import srbscan

pytestmark = pytest.mark.scan

SOURCE_EXT = (".c", ".h", ".S")

#: What State A ships. Computed once at import so the parametrize list is stable,
#: and so a missing mount fails collection loudly rather than producing a suite of
#: zero checks that looks like a clean tree.
def _immutable_sources() -> dict[str, str]:
    root = srbscan.ORIGINAL
    if not root.is_dir():
        return {}
    out = {}
    for top in ("src", "test"):
        for dirpath, dirnames, files in os.walk(root / top):
            dirnames[:] = [d for d in dirnames if d not in srbscan.EXEMPT_DIRS]
            for name in files:
                if not name.endswith(SOURCE_EXT):
                    continue
                p = Path(dirpath) / name
                out[str(p.relative_to(root))] = srbscan.sha256(p)
    # version.h.in is a template rather than a source, and is frozen for the same
    # reason: CMake has to keep substituting it, and an edit shows up in stage 2 as
    # a wrong installed sodium/version.h with no build log explaining why.
    tmpl = "src/libsodium/include/sodium/version.h.in"
    if (root / tmpl).is_file():
        out[tmpl] = srbscan.sha256(root / tmpl)
    return out


CHECKSUMS = _immutable_sources()

#: 352 sources + version.h.in. Stage 2's frozen `data/sources.json` lists the 352
#: and omits the template, so this scan is the only place the template's bytes are
#: counted and compared. The arithmetic is asserted below rather than trusted.
EXPECTED_IMMUTABLE = 353


def test_the_reference_tree_is_mounted():
    """First, because everything below is vacuous without it.

    A scan whose comparisons all pass because it had nothing to compare against
    looks exactly like a clean tree in the rendered digest. This check is what
    makes the difference visible.
    """
    assert srbscan.ORIGINAL.is_dir(), (
        f"State A is not mounted at {srbscan.ORIGINAL}; the source comparison "
        f"below has no reference and reports nothing")
    # Exact, not a floor. libsodium 1.0.20 ships 352 .c/.h/.S files under src/ and
    # test/, plus version.h.in. A wrong number here means the mount is incomplete or
    # the pin moved, and either way the 353 comparisons below are measuring
    # something other than what this task froze.
    assert len(CHECKSUMS) == EXPECTED_IMMUTABLE, (
        f"{len(CHECKSUMS)} immutable files were found in the reference tree at "
        f"{srbscan.ORIGINAL}, expected {EXPECTED_IMMUTABLE}; the mount is "
        f"incomplete or State A is not the pinned 1.0.20")


@pytest.mark.parametrize("relpath", sorted(CHECKSUMS))
def test_source_unmodified(repo, relpath):
    p = repo / relpath
    assert p.is_file(), f"immutable source {relpath} is missing (§3)"
    actual = srbscan.sha256(p)
    assert actual == CHECKSUMS[relpath], (
        f"{relpath} differs from State A: expected sha256 "
        f"{CHECKSUMS[relpath][:16]}, got {actual[:16]} (§3 -- this is a "
        f"build-system migration and no source may be edited)")


def test_no_source_file_added_under_src(repo):
    """New .c/.h/.S under src/ would change what the library is.

    This is the half a checksum cannot see, which is why it is worth having beside
    the 352: the hashes confirm that what State A shipped is intact, and say
    nothing at all about what was added next to it.
    """
    known = set(CHECKSUMS)
    extra = []
    for dirpath, dirnames, files in os.walk(repo / "src"):
        dirnames[:] = [d for d in dirnames if d not in srbscan.EXEMPT_DIRS]
        for f in files:
            if not f.endswith(SOURCE_EXT):
                continue
            rel = os.path.relpath(os.path.join(dirpath, f), repo)
            if rel not in known:
                extra.append(rel)
    assert extra == [], (
        f"source files added under src/: {sorted(extra)[:20]} -- §3 freezes the "
        f"library's sources, and a new translation unit changes what is built")


def test_no_source_file_added_under_test(repo):
    known = set(CHECKSUMS)
    extra = []
    for dirpath, dirnames, files in os.walk(repo / "test"):
        dirnames[:] = [d for d in dirnames if d not in srbscan.EXEMPT_DIRS]
        for f in files:
            if not f.endswith(SOURCE_EXT):
                continue
            rel = os.path.relpath(os.path.join(dirpath, f), repo)
            if rel not in known:
                extra.append(rel)
    assert extra == [], f"source files added under test/: {sorted(extra)[:20]}"


def test_no_source_file_is_a_symlink(repo):
    """A symlink hashes as its target, so it passes every check above.

    Pointing `crypto_box.c` at a file the submission also ships leaves the
    checksum satisfied and changes what the compiler reads, which is one of the
    shapes `sources_untouched` asks the reviewer to look for. Cheap to check here.
    """
    links = []
    for relpath in sorted(CHECKSUMS):
        p = repo / relpath
        if p.is_symlink():
            links.append(f"{relpath} -> {os.readlink(p)}")
    assert not links, (
        f"immutable sources delivered as symlinks: {links[:10]}; a symlink hashes "
        f"as its target and is not the file State A shipped")


def test_no_immutable_source_is_missing(repo):
    """The composite, so a stripped tree is one finding rather than 352.

    A submission that deleted half of src/ fails 176 checks above, and the digest
    the reviewer sees would be nothing but those. This reports the count.
    """
    missing = [r for r in sorted(CHECKSUMS) if not (repo / r).is_file()]
    assert not missing, (
        f"{len(missing)} of {len(CHECKSUMS)} immutable sources are not in the "
        f"delivered tree: {missing[:15]}")


def test_no_generated_version_h_in_source_tree(repo):
    """version.h belongs in the build tree, not the source tree (§1.1, §3)."""
    p = repo / "src/libsodium/include/sodium/version.h"
    assert not p.exists(), (
        "src/libsodium/include/sodium/version.h was generated into the source "
        "tree; §1.1 requires out-of-source builds")


# Not here: a check that this stage's computed manifest agrees with stage 2's
# frozen `data/sources.json`. They should agree, and they do -- but that is a claim
# about the task, not about the submission, and it cannot be made from inside this
# image, which does not ship the frozen file. Written as a test it would skip on
# every run, and `pytest_module` scores an unlicensed skip as a miss, so the suite
# would carry a permanent phantom defect to assert something no submission can
# affect. It is verified once at construction instead, by hashing State A on the
# host and diffing against `behavioural/data/sources.json`.
