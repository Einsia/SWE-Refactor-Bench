"""Did the sources survive the migration?

The measurement in this suite, and the one place a mechanical check is the whole
answer rather than a lead. A sha256 over `gson/src/main/java/com/google/gson/Gson.java`
is not a matter of interpretation: either the delivered bytes are State A's or they
are not, and a submission whose build only works because it edited
`GsonBuildConfig.java` to hardcode the version has changed the thing under test.

The classification is derived from the tree mounted at /opt/original on every run,
not read from a checked-in manifest. Two reasons. A frozen list in this image would
be a second copy of State A's checksums that `_shared_input_drift` does not compare,
so re-cutting the snapshot would leave it stale and every check here would pass
against the wrong reference. And deriving it means the exact-count guard below is
load-bearing: it fails if the mount is missing, which is the failure that would
otherwise turn this module into 241 vacuous passes.

Every check is `scan`-marked and advisory. The prompt tells the review that a
mismatch here is the certain kind of finding -- open the path, confirm the diff,
fail `sources_untouched` -- and that is where it gates.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import srbscan

pytestmark = pytest.mark.scan

#: Where State A keeps content the migration must not rewrite. `lib/` is two
#: IDE style configs, `examples/` a standalone Android sample with its own
#: proguard config; both are delivered files a build script has no reason to touch.
IMMUTABLE_ROOTS = ("gson/src/", "extras/src/", "metrics/src/", "proto/src/",
                   "lib/", "examples/")

#: The five files the migration is supposed to retire. Their continued presence is
#: what `maven_retired` asks about; this module only reports it.
MAVEN_ARTIFACTS = ("pom.xml", "gson/pom.xml", "extras/pom.xml",
                   "metrics/pom.xml", "proto/pom.xml")

#: Files that are State A's wherever they sit outside the frozen roots. Both
#: licences: the project's, and the copy `gson/` ships so the jar can carry it.
IMMUTABLE_EXACT = ("LICENSE", "gson/LICENSE")

#: Paths a migration may legitimately rewrite or delete: the retired POMs, the bnd
#: instructions file (whose directives may reasonably move into the Gradle build),
#: and the project's own prose.
#:
#: Prose means markdown at the repository root or at a subproject root -- the six
#: top-level documents and the four subproject READMEs. Depth is the test rather
#: than the suffix, because `examples/android-proguard-example/README.md` is part of
#: a delivered sample, not documentation of the build, and a submission has no more
#: business rewriting it than it has rewriting the sample's `proguard.cfg` beside it.
MUTABLE_SUFFIXES = (".md", ".markdown")
MUTABLE_NAMES = ("bnd.bnd",)
MUTABLE_MAX_DEPTH = 1


def _classify(root: Path):
    """State A partitioned into (immutable, retired, prose).

    ``immutable`` maps relpath -> sha256 and is the reference for the parametrized
    check. ``retired`` is the Maven files. ``prose`` is everything a migration may
    edit, listed so the count guard can assert the partition covers the tree.
    """
    immutable: dict[str, str] = {}
    retired: list[str] = []
    prose: list[str] = []
    if not root.is_dir():
        return immutable, retired, prose
    for rel in srbscan.rel_files(root):
        path = root / rel
        if rel in MAVEN_ARTIFACTS or Path(rel).name == "pom.xml":
            retired.append(rel)
            continue
        name = Path(rel).name
        shallow = rel.count("/") <= MUTABLE_MAX_DEPTH
        if name in MUTABLE_NAMES or (shallow and rel.endswith(MUTABLE_SUFFIXES)):
            prose.append(rel)
            continue
        if rel.startswith(IMMUTABLE_ROOTS) or rel in IMMUTABLE_EXACT:
            immutable[rel] = srbscan.sha256(path)
            continue
        prose.append(rel)
    return immutable, retired, prose


IMMUTABLE, RETIRED, PROSE = _classify(srbscan.ORIGINAL)


def test_the_reference_tree_is_mounted():
    """241 frozen files, 5 POMs, 11 editable.

    The guard that makes the rest of this module mean something. Without it a
    missing /opt/original mount yields an empty IMMUTABLE, pytest generates zero
    parametrized cases, and the module reports "no failures" for a tree it never
    read. The numbers are State A's, so this also catches a snapshot re-cut that
    changed the inventory without anyone updating the classification.
    """
    assert len(IMMUTABLE) == 241, (
        f"expected 241 immutable files in State A, classified {len(IMMUTABLE)}; "
        f"the reference mount at {srbscan.ORIGINAL} is wrong or the snapshot changed"
    )
    assert len(RETIRED) == 5, f"expected 5 POMs in State A, found {len(RETIRED)}"
    assert len(PROSE) == 11, (
        f"expected 11 editable files (10 markdown + bnd.bnd), found "
        f"{len(PROSE)}: {sorted(PROSE)}"
    )


@pytest.mark.parametrize("relpath", sorted(IMMUTABLE), ids=lambda r: r)
def test_file_unmodified(repo, relpath):
    """``relpath`` is byte-identical to State A.

    241 of these. Each names one file, so a digest of the scan says which file
    changed rather than "sources differ", and the review can open exactly that path.
    """
    expected = IMMUTABLE[relpath]
    delivered = repo / relpath
    if not delivered.exists():
        pytest.fail(f"{relpath}: missing from the submission")
    actual = srbscan.sha256(delivered)
    assert actual == expected, (
        f"{relpath}: content differs from State A "
        f"(State A {expected[:12]}, submitted {actual[:12]})"
    )


def test_no_immutable_file_is_a_symlink(repo):
    """A symlink hashes as its target.

    `sha256` follows it, so `Gson.java -> /opt/original/gson/src/.../Gson.java`
    would pass all 241 checks above while the delivered tree contains no source at
    all -- and would then fail stage 2 confusingly, on a compile error, in a
    container where /opt/original is not mounted.
    """
    links = [rel for rel in IMMUTABLE if (repo / rel).is_symlink()]
    assert not links, (
        f"{len(links)} frozen path(s) delivered as symlinks, which hash as their "
        f"target rather than as content: {sorted(links)[:10]}"
    )


def test_no_immutable_file_is_missing(repo):
    """The composite, for a digest that reads as one line rather than 241."""
    missing = [rel for rel in IMMUTABLE if not (repo / rel).exists()]
    assert not missing, (
        f"{len(missing)} of 241 frozen files absent from the submission: "
        f"{sorted(missing)[:10]}"
    )


def test_no_java_source_added_under_the_frozen_roots(repo, original):
    """No new `.java` under gson/src, extras/src, metrics/src, proto/src.

    The frozen roots are an input, and a submission that added a class there has
    either vendored a dependency it could not resolve or written the code its build
    was supposed to generate. Both are findings for `sources_untouched`; which one
    it is depends on the file, which is why this reports paths and stops.

    Sources *outside* those roots are not reported here. A convention plugin in
    `buildSrc/src/main/groovy/` is Gradle build code and belongs to the build, and
    `buildSrc/src/main/java/` is a legitimate place to put a task implementation.
    """
    def java_under_roots(root: Path) -> set[str]:
        return {rel for rel in srbscan.rel_files(root)
                if rel.endswith(".java") and rel.startswith(IMMUTABLE_ROOTS)}

    added = sorted(java_under_roots(repo) - java_under_roots(original))
    assert not added, (
        f"{len(added)} Java source(s) added under the frozen source roots: "
        f"{added[:10]}"
    )


def test_no_frozen_source_moved_into_a_gradle_layout(repo, original):
    """No frozen file relocated to a path Gradle's defaults would prefer.

    Gson's Maven layout is already `src/main/java`, so Gradle's convention needs no
    move at all -- which makes any relocation of a frozen file a deliberate act.
    The one this looks for is the flattening a submission reaches for when it cannot
    get four subprojects configured: copy `gson/src/main/java/...` to
    `src/main/java/...` and build one flat project. That produces one jar with the
    right classes in it and fails everything stage 2 measures about the other three.
    """
    before = set(srbscan.rel_files(original))
    after = set(srbscan.rel_files(repo))
    moved = []
    for rel in sorted(IMMUTABLE):
        if rel in after:
            continue
        tail = rel.split("/", 1)[1] if "/" in rel else rel
        for candidate in after - before:
            if candidate.endswith(tail):
                moved.append(f"{rel} -> {candidate}")
                break
    assert not moved, (
        f"{len(moved)} frozen source(s) appear relocated rather than edited: "
        f"{moved[:10]}"
    )


def test_the_frozen_roots_still_hold_their_own_sources(repo):
    """Per-root counts, so a digest names the subproject that lost its sources.

    The composite above says "17 files missing"; this says "extras/src lost all 13",
    which is the difference between a reviewer reading a list and understanding a
    submission that gave up on one subproject.
    """
    expected: dict[str, int] = {}
    for rel in IMMUTABLE:
        for root in IMMUTABLE_ROOTS:
            if rel.startswith(root):
                expected[root] = expected.get(root, 0) + 1
                break
    short = []
    for root, count in sorted(expected.items()):
        have = sum(1 for rel in IMMUTABLE
                   if rel.startswith(root) and (repo / rel).exists())
        if have != count:
            short.append(f"{root}: {have}/{count}")
    assert not short, f"frozen roots missing content: {', '.join(short)}"
