"""The 583 files the migration was not allowed to touch.

`src/`, `lib/`, `Doc/` and six named root documents are frozen by the contract.
That is 583 of the snapshot's 590 files: 120 C sources and headers, 289 Python
modules and stubs, 168 documents, six readmes.  One sha256 each, taken against
the reference tree mounted at /opt/original rather than against a checksum file
shipped in this image -- a frozen manifest here would be a second copy of State A
that could drift from the tarball without anything noticing.

Why this is the largest module and the least interesting one: the task is to
replace 47 compiler invocations' worth of build description, and the shortest way
to make a build description simpler is to change what it builds.  Drop
`-DPYCRYPTO_LITTLE_ENDIAN` and edit `endianess.h` to assume it.  Add
`#include <wmmintrin.h>` unconditionally so the AESNI probe stops mattering.  Edit
`Crypto/Util/_raw_api.py` to stop asking for a symbol the new build does not
export.  Every one of those makes the build pass and inverts the exercise, and
each is a one-line diff in a file nobody reads twice.

A hash per file is how that is caught, and it is worth being exact about what the
hash does *not* see.  Four failure modes survive a clean manifest comparison, so
each gets its own check:

  - a file *added* under `src/` or `lib/` (nothing it replaces is modified)
  - a frozen file replaced by a *symlink* to a writable copy
  - a frozen file whose *directory entry* vanished (the manifest only iterates
    what is there, so a deletion is invisible to a per-file loop)
  - a *generated* file dropped into the source tree, which is the migration
    checking its own homework

The parametrized comparison itself is `assert delivered == reference` over bytes.
No tolerance, no normalisation, no "ignoring whitespace" -- a C source that
differs by whitespace differs, and the reviewer can see from the citation whether
it matters.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from srbscan import ORIGINAL, rel_files, sha256

pytestmark = pytest.mark.scan

#: The contract's rule, in code.  Everything under these prefixes, plus the six
#: named files, is frozen.
IMMUTABLE_PREFIXES = ("src/", "lib/", "Doc/")
IMMUTABLE_FILES = (
    "README.rst",
    "LICENSE.rst",
    "AUTHORS.rst",
    "Changelog.rst",
    "FuturePlans.rst",
    "INSTALL.rst",
)


def _immutable_from_mount() -> list[str]:
    """Derive the frozen list from the *mounted* reference tree.

    Read at import time so each file becomes its own check with its own name in
    the result -- `test_frozen_file_is_byte_identical[src/AES.c]` tells a reviewer
    which file moved without reading a traceback.  If the mount is missing this
    returns empty and `test_the_reference_tree_is_mounted` is the check that says
    so; an exception here would take the whole module out and produce no findings
    at all, which reads like a clean tree.
    """
    if not ORIGINAL.is_dir():
        return []
    return [
        rel
        for rel in rel_files(ORIGINAL)
        if rel.startswith(IMMUTABLE_PREFIXES) or rel in IMMUTABLE_FILES
    ]


IMMUTABLE = _immutable_from_mount()

#: The snapshot's own count.  Asserted exactly, not as a floor: a wrong number
#: here means the mount is incomplete or the pin moved, and either way every
#: comparison below is being run against the wrong State A.
EXPECTED_IMMUTABLE = 583


def test_the_reference_tree_is_mounted(original):
    """First, because everything below is vacuous without it."""
    files = rel_files(original)
    assert len(files) == 590, (
        f"{original} holds {len(files)} files, expected 590.  The reference "
        "snapshot is not mounted, or is not pycryptodome 3.20.0 as pinned"
    )


def test_the_frozen_set_is_the_expected_size():
    assert len(IMMUTABLE) == EXPECTED_IMMUTABLE, (
        f"derived {len(IMMUTABLE)} frozen files from the mount, expected "
        f"{EXPECTED_IMMUTABLE}.  Either the mount is partial or the contract's "
        "definition of frozen (src/ lib/ Doc/ and six root documents) has "
        "drifted from what this module implements"
    )


@pytest.mark.parametrize("rel", IMMUTABLE or ["<no reference tree mounted>"])
def test_frozen_file_is_byte_identical(repo, original, rel):
    if not IMMUTABLE:
        pytest.fail("no reference tree mounted; nothing was compared")
    ref = original / rel
    got = repo / rel
    assert got.exists(), f"{rel} was deleted from the submission"
    assert not got.is_symlink(), (
        f"{rel} is a symlink in the submission, pointing at "
        f"{os.readlink(got)!r}.  A symlink hashes as its target, so this is how a "
        "frozen file gets a writable copy"
    )
    want, have = sha256(ref), sha256(got)
    assert have == want, (
        f"{rel} differs from the reference.\n  reference sha256 {want}\n  "
        f"delivered sha256 {have}\n  This file was frozen by the contract.  Open "
        "it and diff it: if it changed, the migration changed what is being "
        "built rather than how it is built"
    )


#: Filenames a build system is entitled to add anywhere, including under a frozen
#: root.
#:
#: This is not a loophole, it is the shape of the tool. Meson has no `subdir()`
#: without a `meson.build` in the subdirectory, and it does not glob -- so a build
#: that installs `lib/Crypto/Cipher/*.py` must have a `lib/Crypto/Cipher/
#: meson.build` naming them. The reference port has 20 of these, one per package
#: directory, and a rule that forbade them would fail every correct submission
#: while passing one that put all 288 source names in the root file.
#:
#: What is still forbidden under those roots is anything the compiler or the
#: interpreter reads: a new `.c`, `.h`, `.py` or `.pyi`. Those are the payload.
BUILD_FILENAMES = ("meson.build", "meson.options", "meson_options.txt")


def test_nothing_was_added_under_the_frozen_roots(repo, original):
    """A hash per reference file cannot see a file that has no reference.

    An added `src/aesni_shim.c` or `lib/Crypto/Util/_compat.py` leaves all 583
    hashes intact, and it is the most natural way to make a stubborn probe or a
    missing symbol go away.
    """
    ref = set(rel_files(original))
    added = sorted(
        rel
        for rel in rel_files(repo)
        if rel.startswith(IMMUTABLE_PREFIXES)
        and rel not in ref
        and Path(rel).name not in BUILD_FILENAMES
    )
    assert not added, (
        f"{len(added)} file(s) appear under src/ lib/ Doc/ that are not in the "
        "reference tree and are not build descriptions:\n  "
        + "\n  ".join(added[:30])
    )


def test_added_build_files_under_frozen_roots_are_only_build_files(repo, original):
    """Where the exemption above was used, and how widely.

    Reported rather than forbidden, because 20 `meson.build` files under
    `lib/Crypto/` is what the reference port looks like. What this catches is the
    exemption being used for something else: a `meson.build` under `src/` that is
    2000 lines of generated source, or one per file rather than one per directory.
    A reviewer reading this finding sees the count and the paths and can judge in
    one look; the `meson_is_the_build` gate is where that judgement lands.
    """
    ref = set(rel_files(original))
    added = sorted(
        rel
        for rel in rel_files(repo)
        if rel.startswith(IMMUTABLE_PREFIXES) and rel not in ref
    )
    if not added:
        return
    oversized = [
        rel for rel in added
        if len((repo / rel).read_bytes() if (repo / rel).is_file() else b"") > 65536
    ]
    assert not oversized, (
        "a build file added under a frozen root is larger than 64 KiB:\n  "
        + "\n  ".join(oversized)
        + "\n  Meson's explicit source lists are long, but this is long enough to "
        "be generated output or a transcribed source file rather than a build "
        "description"
    )


def test_no_frozen_directory_disappeared(repo, original):
    """Directories, separately from files.

    An empty `lib/Crypto/Cipher/` with its contents gone would fail 40 individual
    comparisons, which is correct but reads as forty findings.  This says it once,
    at the level a reviewer can act on.
    """
    ref_dirs = {
        str(Path(rel).parent)
        for rel in rel_files(original)
        if rel.startswith(IMMUTABLE_PREFIXES)
    }
    missing = sorted(d for d in ref_dirs if not (repo / d).is_dir())
    assert not missing, (
        f"{len(missing)} frozen directory(ies) are gone from the submission:\n  "
        + "\n  ".join(missing[:30])
    )


def test_no_generated_output_inside_the_source_tree(repo):
    """Compiled and configured output under `src/` or `lib/`.

    Two distinct problems wearing one shape.  A committed `.o` or `.abi3.so` means
    the build does not have to work for the import to work, and stage 2 would
    measure a build that never ran.  A committed `config.h`-alike means a probe
    the build system was supposed to answer at configure time was answered once,
    by hand, off this machine.
    """
    suffixes = (".o", ".a", ".so", ".pyd", ".dylib", ".obj", ".lo", ".la",
                ".gcda", ".gcno", ".d")
    hits = []
    for root in ("src", "lib"):
        base = repo / root
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and (
                path.suffix in suffixes or ".abi3." in path.name
            ):
                hits.append(str(path.relative_to(repo)))
    assert not hits, (
        f"{len(hits)} build output file(s) live inside the source tree:\n  "
        + "\n  ".join(sorted(hits)[:30])
    )


# A check comparing this module's live manifest against the frozen baseline.json
# that tests/behavioural/data/ carries is deliberately absent.  That file is not in
# this image -- `_shared_input_drift` (cli.py:291) would flag the duplicate
# basename, and shipping it anyway would give this stage a second, unversioned
# claim about State A.  A check that skipped when the file is missing would skip
# every single run, and `swerefactor.pytest_module` rewrites an unlicensed skip to a
# failure, so it would read as a miss forever.
