#!/usr/bin/env python3
"""Which files moved against State A, and which did not.

The change set is the first thing a reviewer of a port wants and the last thing it
should have to assemble by hand. This module hashes every file State A shipped
against the submission's copy of it, one check per file, and reports what was added
and what was deleted.

What this is *not* is a requirement about the change set. There is no list of files
a byte-order port is allowed to touch:

  * A detection can live in `cutils.h`, in a new header, or nowhere in the source
    at all if the build defines the macro per target.
  * The bootstrap fix is in the `Makefile`, and a submission that reorganised the
    build to express it has touched more.
  * Adding tests is allowed and encouraged, so new files under `tests/` are
    ordinary.

So every check here reports rather than demands, and the numbers are the point. A
submission that changed 6 files and one that changed 200 are different objects, and
the second needs reading before any gate is answered -- which is exactly the
sentence the prompt puts in front of the review.

The one thing worth calling out as close to certain: a file State A shipped that is
now a *symlink*. That is how a source file gets replaced without its bytes changing
in a way a naive hash of the resolved path would notice, and it is a lead for
`port_is_genuine` rather than a verdict.
"""

from __future__ import annotations

import pytest

import srbscan

pytestmark = pytest.mark.scan


# Hashing every file in both trees once, at collection time, keeps the per-file
# checks cheap and lets the parametrize list derive from the mounted reference
# tree rather than from a frozen manifest.
def _pairs():
    original = srbscan.ORIGINAL
    if not original.is_dir():
        return []
    return srbscan.rel_files(original)


ORIGINAL_FILES = _pairs()


@pytest.mark.parametrize("rel", ORIGINAL_FILES)
def test_file_against_state_a(rel, repo, original):
    """One check per file State A shipped: unchanged, changed, or gone.

    A `fail` here means "this file is not what State A delivered", which for this
    task is expected of a handful of files and suspicious of two hundred. The
    verdict is advisory; the reviewer is told to read the set rather than count it.
    """
    src = original / rel
    dst = repo / rel
    if not dst.exists():
        pytest.fail(f"{rel}: delivered tree has no such file (State A shipped it)")
    if dst.is_symlink() and not src.is_symlink():
        pytest.fail(f"{rel}: delivered as a symlink to "
                    f"{dst.readlink()!s}; State A shipped a regular file")
    before = srbscan.sha256(src)
    after = srbscan.sha256(dst)
    if before != after:
        pytest.fail(f"{rel}: content differs from State A "
                    f"({before[:12]} -> {after[:12]})")


def test_no_state_a_file_deleted(repo, original):
    """Files State A shipped that are absent from the submission.

    Reported as one check rather than one per file so a submission that deleted a
    directory produces a readable finding instead of four hundred.
    """
    missing = [rel for rel in ORIGINAL_FILES if not (repo / rel).exists()]
    if missing:
        shown = "\n  ".join(missing[:40])
        more = f"\n  ... and {len(missing) - 40} more" if len(missing) > 40 else ""
        pytest.fail(f"{len(missing)} file(s) State A shipped are not in the "
                    f"submission:\n  {shown}{more}")


def test_files_added(repo, original, delivered_files):
    """Files in the submission that State A did not ship.

    A lead, not a fault. A detection header, a test, a build fragment and a note
    are all ordinary. What the reviewer is looking for in this list is a vendored
    tree, a generated source committed rather than generated, or a second
    implementation.
    """
    known = set(ORIGINAL_FILES)
    added = [rel for rel in delivered_files if rel not in known]
    if added:
        shown = "\n  ".join(added[:60])
        more = f"\n  ... and {len(added) - 60} more" if len(added) > 60 else ""
        pytest.fail(f"{len(added)} file(s) added against State A:\n  {shown}{more}")


def test_change_set_summary(repo, original, delivered_files):
    """The counts, as one finding, so the prompt always carries the shape.

    This check fails whenever anything changed, which is to say: on every real
    submission. That is intentional and it is why nothing here scores -- the point
    is to put "6 files changed, all in the set a byte-order port would touch" or
    "203 files changed" into the review's prompt as a sentence it cannot miss.
    """
    known = set(ORIGINAL_FILES)
    delivered = set(delivered_files)
    changed, missing = [], []
    for rel in ORIGINAL_FILES:
        dst = repo / rel
        if not dst.exists():
            missing.append(rel)
        elif srbscan.sha256(dst) != srbscan.sha256(original / rel):
            changed.append(rel)
    added = sorted(delivered - known)

    core = [r for r in changed if r in srbscan.CORE_SOURCES]
    build = [r for r in changed if r == "Makefile" or r.endswith((".mk", ".m4",
                                                                  ".ac", ".am"))]
    other = [r for r in changed
             if r not in core and r not in build]

    if not (changed or added or missing):
        return  # a do-nothing submission; the gates will say so

    lines = [
        f"State A: {len(ORIGINAL_FILES)} files. "
        f"Submission: {len(delivered)} files.",
        f"changed: {len(changed)}   added: {len(added)}   deleted: {len(missing)}",
        "",
        f"changed interpreter/runtime sources ({len(core)}): "
        + (", ".join(core) or "none"),
        f"changed build files ({len(build)}): " + (", ".join(build) or "none"),
        f"changed other ({len(other)}): "
        + (", ".join(other[:30]) or "none")
        + (f" ... +{len(other) - 30}" if len(other) > 30 else ""),
        f"added ({len(added)}): "
        + (", ".join(added[:30]) or "none")
        + (f" ... +{len(added) - 30}" if len(added) > 30 else ""),
    ]
    pytest.fail("\n".join(lines))
