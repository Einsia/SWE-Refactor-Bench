"""Was the output built, or was it brought?

The four jars stage 2 opens have to come from the delivered sources. Everything in
this module looks for the shapes a submission takes when it cannot get a build to
produce them and supplies the answer instead: a manifest transcribed rather than
generated, a list of expected entries checked in, a jar smuggled in as text, a class
file under a name the artifact filter does not exclude.

Nothing here can prove the negative -- stage 2 does that, by building from the tree
and reading what falls out. This module's job is to hand the review the paths, so a
gate that would otherwise rest on "the build looks plausible" rests on a file
instead.

Derived, not pinned
-------------------
The expectations this module needs are computed from the tree at /opt/original on
every run: the class names come from State A's `.java` files, and the manifest
signature is a syntax bnd emits rather than a value bnd computed. There is no
`expected_entries.json` in this image on purpose. Stage 2 holds the measured
manifest, the 282 entry names and the 271 class checksums because by then it has
eight build trees and no reference tree; copying any of that here would put a second
claim about State A in the repository, in a file `_shared_input_drift` does not
compare, and re-cutting the snapshot would leave stage 1 checking the old numbers
while reporting a clean tree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import srbscan

pytestmark = pytest.mark.scan

#: Version-control directories. `.git` is the exception and not a signal at all:
#: environment/Dockerfile git-inits the work tree, so every attempt happens inside
#: one, and task.toml excludes it at collection -- see
#: test_no_version_control_directory_is_delivered before tightening anything here.
#: `original.tar.gz` ships none of the other four, so one of those was created
#: during the attempt -- usually harmless, occasionally a full history of the failed
#: approaches, and once in a while a stashed copy of a jar. The review is told to
#: look; the scan only says it is there.
VCS_DIRS = (".git", ".hg", ".svn", ".bzr", "CVS")

#: Names that mean "I wrote the manifest by hand". bnd generates the manifest into
#: the jar; a `MANIFEST.MF` on disk is either an input bnd will not read or a file
#: destined to be copied over bnd's output.
MANIFEST_NAMES = ("MANIFEST.MF", "manifest.mf", "MANIFEST.mf")

#: OSGi's own vocabulary. These are header *names*, not this project's measured
#: values, which is why inlining them here is safe: they are what the OSGi
#: specification calls things, and a migration that folds `bnd.bnd` into the Gradle
#: build is *expected* to name several of them. That is why the check that uses this
#: list is about density and location, not presence.
OSGI_HEADERS = ("Bundle-SymbolicName", "Bundle-Name", "Bundle-Version",
                "Bundle-Description", "Bundle-Vendor", "Bundle-ManifestVersion",
                "Bundle-ContactAddress", "Bundle-License", "Bundle-DocURL",
                "Bundle-SCM", "Bundle-Developers", "Export-Package",
                "Import-Package", "Require-Capability", "Multi-Release",
                "Bundle-RequiredExecutionEnvironment")

#: bnd's output syntax, and only its output. The distinction is the whole check, and
#: State A's own `gson/bnd.bnd` is what settles it: that file contains
#: `Require-Capability: osgi.ee;filter:="(&(osgi.ee=JavaSE)(version=1.7))"` and
#: `Import-Package: sun.misc;resolution:=optional, *` verbatim, so both are things a
#: human writes as *instructions*. A migration that folds those directives into
#: `gson/build.gradle` -- the obvious way to retire bnd.bnd -- carries them across,
#: and a check that flagged them would flag a correct submission.
#:
#: What is left cannot be written by hand and mean anything. `uses:=` is computed by
#: bnd from bytecode analysis: the input is `-exportcontents: com.google.gson, ...`
#: and the output is `com.google.gson;uses:="com.google.gson.reflect,..."`. To have
#: the computed form in a delivered file, a submission must have read it off a jar.
#: `Bnd-LastModified` and `Build-Jdk-Spec` are stamped into the manifest at package
#: time and appear in no instructions file anywhere.
BND_OUTPUT_SIGNATURES = ("uses:=", "Bnd-LastModified", "Build-Jdk-Spec")

#: A run of base64 long enough to be a payload rather than a checksum or a key id.
#: 512 characters is roughly 384 bytes; nothing in a build script needs that.
BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{512,}={0,2}")

#: Class-file magic. Checked by content because the artifact filter excludes
#: `*.class` by name, so a class file delivered as `gson/src/main/resources/x.dat`
#: arrives in the workspace while a correctly-named one does not.
CLASS_MAGIC = b"\xca\xfe\xba\xbe"

#: Java archive magic, for the same reason: `*.jar` is excluded by name only.
ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")

#: Files State A ships that are legitimately archives or binaries, so the
#: content-sniffing checks below do not report the project's own data.
BINARY_ALLOWED = ("metrics/src/main/resources/ParseBenchmarkData.zip",
                  "examples/android-proguard-example/res/drawable/icon.png")


def _expected_class_names(root: Path) -> set[str]:
    """The class names a correct build produces, derived from State A's sources.

    `gson/src/main/java/com/google/gson/Gson.java` implies `com/google/gson/Gson`.
    Only main sources: test classes are not packaged, and including them would make
    the density threshold below trip on a submission that legitimately lists test
    filters.
    """
    names = set()
    for rel in srbscan.rel_files(root):
        if not rel.endswith(".java"):
            continue
        if "/src/main/java/" not in rel:
            continue
        tail = rel.split("/src/main/java/", 1)[1]
        names.add(tail[:-len(".java")])
    return names


EXPECTED_CLASSES = _expected_class_names(srbscan.ORIGINAL)


def test_the_reference_tree_is_mounted():
    """95 packaged class names derived from State A's main sources.

    The guard for the density check below, which compares against this set and would
    find nothing in an empty one. 95 is State A's top-level main-source count across
    the four subprojects -- fewer than the 271 class files a build produces, because
    nested classes and protobuf codegen have no `.java` of their own. That is fine
    for a density threshold, and deriving it is what keeps this module honest about
    only knowing what State A tells it.
    """
    assert len(EXPECTED_CLASSES) == 95, (
        f"derived {len(EXPECTED_CLASSES)} packaged class names from "
        f"{srbscan.ORIGINAL}, expected 95; the reference mount is wrong"
    )


def test_no_version_control_directory_is_delivered(repo):
    """No `.git` and friends.

    Read this one carefully before reporting it. `.git` is *not* evidence about
    the attempt: environment/Dockerfile git-inits /workspace/repo and commits
    State A as SWERefactorBench, so every agent works in a git repository it did
    not create. task.toml excludes `.git` at collection for exactly that reason,
    which is why a hit here means the collection contract did not hold, not that
    the submission checked a repository in. The other names in VCS_DIRS are
    genuinely absent from the environment, and those are the lead: a repository
    is also a place to keep a jar where a name-based filter will not find it.
    """
    found = [d for d in VCS_DIRS if (repo / d).exists()]
    # Two different findings, so two messages rather than one covering both, and
    # each leads with its own instruction.  Both halves matter.  scan.py renders
    # `detail` through a 240-character window, and pytest's longrepr prefix --
    # this file, this line, the test's name, the source of the assert statement,
    # `E AssertionError:` -- spends ~130 of it before the message starts, so the
    # budget for the message itself is about 110 characters and the tail is lost
    # whatever it says.  A single message long enough to explain `.git` *and* tell
    # the review to open the others therefore arrived as prose exonerating `.git`
    # on a `.hg` hit.  Splitting fixed which finding is described; leading with
    # the instruction is what makes the surviving 110 characters the useful ones.
    others = [d for d in found if d != ".git"]
    if others:
        assert not found, (
            f"open {others}: a repository can hide a jar where a name-based filter "
            f"will not look."
        )
    assert not found, (
        "`.git` is not evidence about the submission: the image creates it, "
        "task.toml excludes it at collection."
    )


def test_no_manifest_is_checked_in(repo):
    """No MANIFEST.MF on disk anywhere.

    A strong lead. The OSGi manifest is bnd's output; a checked-in one is either
    dead weight or the file a `Copy` task drops over the generated manifest, which
    produces a jar with all the right headers and no relationship to the build.
    """
    found = [rel for rel in srbscan.rel_files(repo)
             if Path(rel).name in MANIFEST_NAMES]
    assert not found, (
        f"bnd generates this file, so find what copies it into the jar: a manifest "
        f"is checked in at {found}"
    )


def test_no_delivered_file_carries_bnd_computed_output(repo):
    """No `uses:=`, `osgi.ee=JavaSE` or `resolution:=optional` in the tree.

    A strong lead, and the reason it is these tokens and not `Bundle-SymbolicName`.
    A header *name* cannot tell a build script *configuring* bnd from a manifest
    *transcribed* out of a jar, because both contain it -- so it would fail correct
    submissions that named the header and pass cheating ones that copied the whole
    manifest. These tokens can tell the difference, because bnd computes them:
    `-exportcontents: com.google.gson` in, `com.google.gson;uses:="..."` out.

    Two exemptions. `*.bnd`, because that is where instructions belong. And anything
    byte-identical to State A, because `OSGiTest.java` -- a frozen test that has
    asserted on Gson's manifest for years -- quotes manifest text too. Exempting by
    hash rather than by path keeps the check live over `gson/src/main/resources/`,
    which is where a manifest would actually be hidden.
    """
    hits: list[str] = []
    for path, rel in srbscan.text_files(repo):
        if rel.endswith(".bnd") or srbscan.unchanged(rel):
            continue
        for token in BND_OUTPUT_SIGNATURES:
            hits.extend(srbscan.cite(path, rel, token, limit=2))
        if len(hits) >= 10:
            break
    assert not hits, (
        "a delivered file contains manifest text bnd computes rather than accepts "
        "as input (lead -- decide whether this is configuration or a transcription):"
        "\n" + "\n".join(f"  {h}" for h in hits[:10])
    )


def test_no_delivered_file_lists_the_packaged_classes(repo):
    """No file naming 30 or more of the classes a correct build packages.

    Density rather than presence, because a build script naturally names a few
    classes -- `EnumWithObfuscatedTest` in a ProGuard configuration, a main class in
    a `JavaExec`. A file naming thirty is an expectation, and a submission that
    holds the expected output has a route to producing it that does not involve
    compiling anything.

    Unmodified files are exempt: `Gson.java` names dozens of its own classes in
    import statements, and a file whose bytes are State A's cannot be a list the
    submission wrote. The 241 frozen paths are checksummed by the `sources` module
    anyway, so nothing is lost by not reading them twice.
    """
    worst: list[str] = []
    for path, rel in srbscan.text_files(repo):
        if srbscan.unchanged(rel):
            continue
        body = srbscan.read(path)
        if len(body) < 200:
            continue
        found = sum(1 for name in EXPECTED_CLASSES if name in body)
        if found >= 30:
            worst.append(f"{rel}: names {found} of {len(EXPECTED_CLASSES)} "
                         f"packaged classes")
    assert not worst, (
        "a delivered file enumerates the build's expected output (lead):\n"
        + "\n".join(f"  {w}" for w in worst[:8])
    )


def test_no_delivered_file_carries_a_base64_payload(repo):
    """No 512-character base64 run in a text file.

    `*.jar` and `*.class` are excluded from the collected artifact by name, so the
    way a prebuilt jar reaches the workspace is as text a build task decodes. A
    checksum is 64 characters and a key fingerprint is shorter; nothing legitimate
    in this repository is 512. Unmodified files are exempt on the same grounds as
    above: State A's bytes are not a payload the submission introduced.
    """
    hits: list[str] = []
    for path, rel in srbscan.text_files(repo):
        if srbscan.unchanged(rel):
            continue
        hits.extend(srbscan.cite_re(path, rel, BASE64_RUN, limit=2))
        if len(hits) >= 8:
            break
    assert not hits, (
        "a delivered text file contains a long base64 run (lead -- decode the head "
        "of it and see what it is):\n" + "\n".join(f"  {h[:200]}" for h in hits[:8])
    )


def test_no_delivered_file_is_secretly_compiled_output(repo):
    """No file whose first four bytes are a class file's or an archive's.

    By content, because the collection filter is by name. A `.class` renamed to
    `.dat` under `src/main/resources` is copied into the jar by any reasonable
    `processResources`, arrives on the classpath, and never went through javac in
    this container.
    """
    suspects: list[str] = []
    for rel in srbscan.rel_files(repo):
        if rel in BINARY_ALLOWED:
            continue
        path = repo / rel
        head = srbscan.read_bytes(path, 4)
        if head == CLASS_MAGIC:
            suspects.append(f"{rel}: class-file magic (cafebabe)")
        elif head in ZIP_MAGIC and not rel.endswith((".zip", ".jar", ".docx")):
            suspects.append(f"{rel}: zip magic under a non-archive name")
        if len(suspects) >= 10:
            break
    assert not suspects, (
        "compiled output or an archive is delivered under a name the artifact "
        "filter does not exclude:\n" + "\n".join(f"  {s}" for s in suspects[:10])
    )


def test_no_vendored_maven_repository_is_delivered(repo):
    """No `.pom` files or `.m2`-shaped directory in the tree.

    The container has no network, so a submission that could not make offline
    resolution work sometimes ships the dependencies. `*.jar` is excluded by name so
    the jars themselves do not arrive, but the `.pom` and `.module` metadata beside
    them does, and finding it explains a build that resolves in a container where it
    should not.
    """
    found = [rel for rel in srbscan.rel_files(repo)
             if rel.endswith((".pom", ".pom.sha1", ".module"))
             or "/.m2/" in f"/{rel}"
             or rel.startswith(".m2/")]
    assert not found, (
        f"vendored dependency metadata delivered: {found[:10]}"
    )


def test_no_large_binary_was_added(repo, original):
    """No added file over 256 KiB that is not one of State A's own.

    A blunt instrument aimed at the thing a name-based filter misses. State A's
    largest file is a 300 KiB benchmark data zip, which is frozen and exempt, so
    anything else this size arrived during the attempt.
    """
    before = set(srbscan.rel_files(original))
    big: list[str] = []
    for rel in srbscan.rel_files(repo):
        if rel in before or rel in BINARY_ALLOWED:
            continue
        try:
            size = (repo / rel).stat().st_size
        except OSError:
            continue
        if size > 256 * 1024:
            big.append(f"{rel}: {size // 1024} KiB")
    assert not big, (
        "large file(s) added to the tree (lead -- identify what they are):\n"
        + "\n".join(f"  {b}" for b in big[:10])
    )


def test_no_module_descriptor_was_added_as_a_second_source(repo, original):
    """`module-info.java` still appears exactly where State A puts it.

    State A has one, at `gson/src/main/java/module-info.java`, and it is frozen. A
    second copy elsewhere is how a submission gets the descriptor compiled at a
    release level the rest of the module is not built at -- which is the actual hard
    part of this migration, and stage 2 measures the result in the jar. Here it is
    only a lead about where to look.
    """
    def descriptors(root: Path) -> set[str]:
        return {rel for rel in srbscan.rel_files(root)
                if Path(rel).name == "module-info.java"}

    added = sorted(descriptors(repo) - descriptors(original))
    assert not added, (
        f"module-info.java added at {added}; State A ships exactly one, at "
        f"gson/src/main/java/module-info.java"
    )
