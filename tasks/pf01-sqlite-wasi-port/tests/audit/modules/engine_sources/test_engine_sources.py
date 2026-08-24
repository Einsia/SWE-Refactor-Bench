"""Every file State A shipped, and whether it still matches.

One check per file, derived from the tree mounted at SRB_ORIGINAL rather than from
a frozen manifest -- see srbscan.py for why there is no manifest in this image.

Nothing here declares a file immutable, and that is the design rather than an
omission. The platform layer is the subject of the task and is *supposed* to
change; which files that legitimately reaches is a judgement about SQLite's
internals, not a list somebody can write down in advance. A port needs at minimum
to delete two files, add a platform layer and edit the build; depending on how it
handles threading, entropy and temp-file naming it may reasonably touch the mutex
selection, the randomness source or the config header, and a list that guessed
wrong about any of those would fail a correct port for changing a file the author
of this suite did not anticipate.

So the mechanical part is reported and the judgement is left to the reviewer, which
is the split this whole stage is built on: the scan says "these eleven files differ
from State A", the `engine_untouched` gate asks whether each one is platform layer
or engine.

What is *not* a judgement call is that the reviewer should not have to hash 1,856
files to find the eleven.
"""

from __future__ import annotations

import pytest
import srbscan as S

# `srb_skip_ok`: the last check here stands down when State A is not mounted,
# because the first check already reports that and one finding about a broken mount
# is more use than two.  Without the licence, `pytest_module` records that skip as
# `verdict=fail, summary="skipped"`.
pytestmark = [pytest.mark.scan, pytest.mark.srb_skip_ok]

# Derived at collection time from the mounted reference tree.  An empty list when
# nothing is mounted, which collects one placeholder item -- the state the image's
# collect-check.sh phase 2 exists to distinguish from a clean comparison.
STATE_A_FILES = S.rel_files(S.ORIGINAL) if S.ORIGINAL.is_dir() else []

# Files whose content is expected to differ and whose difference says nothing.  Two
# entries, both about the build rather than about the source:
#
#   Makefile / Makefile.in / main.mk -- the build is what the task rewrites.
#   configure -- State A ships a generated one; a port may edit or delete it.
#
# They are still hashed and still reported; they are marked so the reviewer can see
# at a glance which differences were expected. This is not an exemption: an
# `EXPECTED_TO_DIFFER` entry that turned out to hold a vendored POSIX layer would be
# caught by the platform_layer module, which does not consult this list.
EXPECTED_TO_DIFFER = ("Makefile.in", "main.mk", "configure", "configure.ac",
                      "Makefile.msc", "src/os_unix.c", "src/os_win.c",
                      "src/os_win.h")


def _digest_pairs(rel: str):
    return S.sha256(S.ORIGINAL / rel), S.sha256(S.REPO / rel)


def test_the_reference_tree_is_mounted(original, state_a):
    """Without State A there is nothing to compare against.

    This is the check that makes the rest of the module meaningful, and it is why
    it is first: 1,856 comparisons against a missing tree would produce 1,856
    identical failures, which reads as a submission that rewrote everything.
    """
    if len(state_a) <= 1000:
        S.flag(f"the reference tree at {S.ORIGINAL} holds {len(state_a)} file(s), "
               f"not the payload's 1,856 -- read this before any file-level finding "
               f"in this module",
               note="Every comparison in this module is against that tree. A partial "
                    "or missing mount makes all of them meaningless, and makes a "
                    "faithful submission look like one that rewrote everything.")


@pytest.mark.parametrize("rel", STATE_A_FILES)
def test_file_from_state_a_is_unchanged(repo, original, rel):
    """One check per file State A shipped.

    Three outcomes, all reported as this check failing, distinguished in the
    message: the file is gone, the file differs, the file is now a symlink. Which
    of the three matters is the reviewer's call.
    """
    src = original / rel
    dst = repo / rel
    tag = " [expected: build file or retired layer]" if rel in EXPECTED_TO_DIFFER else ""
    note = ("Open both copies and decide what the file is. A platform-layer file is "
            "supposed to change; an edit to the parser, the pager or the b-tree to "
            "make a build problem go away is what the `engine_untouched` gate fails "
            "on.")

    if not dst.exists():
        S.flag(f"{rel} was shipped by State A and is not delivered{tag}", note=note)
    if dst.is_symlink():
        S.flag(f"{rel} is delivered as a symlink to {dst.readlink()}{tag}",
               note="A source file replaced by a link is one whose content the build "
                    "reads from somewhere else. Follow it: inside the repository it "
                    "is a layout choice, outside it the build does not compile this "
                    "tree.")
    a, b = S.sha256(src), S.sha256(dst)
    if a != b:
        S.flag(f"{rel} differs from State A's copy{tag} "
               f"(State A {a[:12]} {src.stat().st_size}B, "
               f"delivered {b[:12]} {dst.stat().st_size}B)", note=note)


def test_no_immutable_file_became_a_directory(repo, original, state_a):
    """The one shape the per-file check reads as 'missing' for the wrong reason."""
    swapped = [rel for rel in state_a if (repo / rel).is_dir()]
    if swapped:
        S.flag("State A shipped these as files and the submission delivers them as "
               "directories", swapped,
               note="The per-file check above reports each of these as 'not "
                    "delivered', which is true and is not the interesting part.")


def test_files_the_submission_added(repo, original, delivered, state_a):
    """What is here that State A did not ship.

    Expected to find things -- a port adds a platform layer and a build script, so
    the empty case would be the surprising one -- and reported as a failure so the
    list reaches the prompt. The list is the useful part: it is where a vendored
    second copy of SQLite, a committed amalgamation, or a source file added to make
    the build work would show up, and it is short enough to read.
    """
    known = set(state_a)
    added = sorted(p for p in delivered if p not in known)
    if added:
        S.flag(f"{len(added)} file(s) are delivered that State A did not ship",
               S.render(added, limit=12),
               note="A platform layer and a build script are expected here, so this "
                    "check finding nothing would be the surprising outcome. What is "
                    "worth reading is anything that looks like a second copy of "
                    "something -- a vendored sqlite/ directory, a committed "
                    "sqlite3.c, a src/ file with a familiar name -- and anything "
                    "under a directory State A did not have. The full list is in "
                    "this module's result file.")


def test_the_delivered_tree_is_not_a_wholesale_replacement(repo, original,
                                                           delivered, state_a):
    """A port edits a repository.  A rewrite is a different submission.

    Loose on purpose: 90% of 1,856 files still matching is a low bar that a genuine
    port clears by a mile -- the expected number of changed files is in single
    figures -- and that a tree assembled from somewhere else fails outright. It
    exists so that "everything differs" arrives as one finding rather than as 1,800.
    """
    if not state_a:
        pytest.skip("State A is not mounted; the first check reports that")
    same = sum(1 for rel in state_a
               if (repo / rel).is_file() and not (repo / rel).is_symlink()
               and S.sha256(repo / rel) == S.sha256(original / rel))
    ratio = same / len(state_a)
    if ratio < 0.90:
        S.flag(f"only {same} of {len(state_a)} files State A shipped are still "
               f"byte-identical ({ratio:.1%}) -- read this before the file-by-file "
               f"findings",
               note="A platform port changes a handful of files; the expected number "
                    "is in single figures. A tree where hundreds differ is either a "
                    "different checkout of SQLite or a repository that was "
                    "regenerated wholesale, and the per-file findings are not worth "
                    "reading until that is explained.")
