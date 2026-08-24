#!/usr/bin/env python3
"""Every jar entry State A produced, checked one at a time.

State A's four jars contain 282 non-directory entries between them.  Each is
checked individually rather than as a set comparison, because "which entry is
missing" is the only useful answer when a migration drops one: a missing
``META-INF/versions/9/module-info.class`` means moditect was never replaced, a
missing ``com/google/gson/internal/GsonBuildConfig.class`` means the source
template was never filtered, and a missing ``srb-probe.properties`` in the
`derive` module's tree means resources are being listed rather than processed.

These read the `assemble` configuration, not `full`.  The distinction is what
keeps one failure from being counted twice: `assemble` packages without running
the test task, so a submission whose jars are byte-correct but whose one flaky
test fails still passes all 282 of these.  Whether the tests pass is the `tests`
module's subject and is scored there once.

The eight ``maven_only_entries``
-------------------------------
``META-INF/maven/<group>/<artifact>/pom.xml`` and its ``pom.properties``, two per
jar.  Maven writes them; Gradle does not.  They are checked here and recorded at
weight 0.0, because their expected answer was *not* recorded from State A -- it is
the opposite of what State A produces.  Every other check in this module says "State
A's jar had this entry, does yours"; those eight say "State A's jar had this entry,
it had better be gone".  The second is a statement about whether the migration
happened, and it is asked once, in stage 1, by ``maven_retired``, where a failure
scores the whole submission zero.  Recording them here still pays for itself: a
report that shows the pom pair surviving explains a Gradle build that copied
Maven's output instead of producing its own.
"""
import json
import os

import pytest

DATA = os.path.join(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"), "data")
with open(os.path.join(DATA, "jars.json")) as _fh:
    JARS = json.load(_fh)

# (artifact key, entry) for every entry State B must contain.
ENTRY_CASES = []
for _key in sorted(JARS):
    for _entry in JARS[_key]["required_entries"]:
        ENTRY_CASES.append((_key, _entry))

# The Maven-only entries, which must be absent.
ABSENT_CASES = []
for _key in sorted(JARS):
    for _entry in JARS[_key].get("maven_only_entries", []):
        ABSENT_CASES.append((_key, _entry))


@pytest.mark.behavioural
@pytest.mark.parametrize("key,entry", ENTRY_CASES,
                         ids=["%s:%s" % (k, e) for k, e in ENTRY_CASES])
def test_entry_present(jars, key, entry):
    """This entry exists in the produced jar."""
    jar = jars.require(key)
    assert jar.has(entry), "%s is missing %s" % (
        os.path.basename(jar.path), entry)


@pytest.mark.srb_weight(0.0)
@pytest.mark.behavioural
@pytest.mark.parametrize("key,entry", ABSENT_CASES,
                         ids=["%s:%s" % (k, e) for k, e in ABSENT_CASES])
def test_maven_only_entry_absent(jars, key, entry):
    """This entry exists only because Maven built State A's jar.

    Recorded at weight 0: State A's jars contain these, and State A is the jar
    every other check in this module compares against. `maven_retired` in stage 1
    owns the question of whether Maven is gone. See the module docstring.
    """
    jar = jars.require(key)
    assert not jar.has(entry), (
        "%s still contains %s, which only Maven produces"
        % (os.path.basename(jar.path), entry))


@pytest.mark.audit
@pytest.mark.parametrize("key", sorted(JARS))
def test_entry_set_exact(jars, data, key):
    """The whole set at once, for a readable summary alongside the 282 singles.

    `maven_only_entries` are excluded from the `extra` set rather than counted as a
    difference. They are State A's `META-INF/maven/**` pom pair -- present in the
    oracle by construction -- and whether they survived is the weight-0 question
    above, not this check's. Anything else unexpected in the jar is still a failure
    here: this is the only check that notices an entry nobody listed.
    """
    jar = jars.require(key)
    want = set(data["jars"][key]["required_entries"])
    tolerated = set(data["jars"][key].get("maven_only_entries", []))
    got = set(jar.entries)
    missing, extra = sorted(want - got), sorted(got - want - tolerated)
    assert not missing and not extra, (
        "%s entry set differs from State A\n  missing (%d): %s\n  extra (%d): %s"
        % (data["jars"][key]["jar_name"], len(missing), missing[:10],
           len(extra), extra[:10]))


@pytest.mark.behavioural
@pytest.mark.parametrize("key", sorted(JARS))
def test_jar_named_as_state_a_named_it(jars, data, key):
    """The file name, charged exactly once.

    ``primary_jar()`` falls back to the only non-sidecar jar a project produced,
    so a correct jar under the wrong name still answers all 282 entry checks and
    is charged here alone.  One naming mistake should cost one check.
    """
    want = data["jars"][key]["jar_name"]
    path = jars.path_of(key)
    assert path is not None, (
        "the %s project produced no jar; expected %s" % (key, want))
    assert os.path.basename(path) == want, (
        "the jar is %s; State A published %s"
        % (os.path.basename(path), want))


# --------------------------------------------------------------- entry content
@pytest.mark.behavioural
@pytest.mark.parametrize("key", sorted(JARS))
def test_no_empty_entries(jars, key):
    """A zero-length .class or resource means something wrote a placeholder."""
    jar = jars.require(key)
    empty = [e for e in jar.entries
             if jar.size(e) == 0 and not e.endswith("/")]
    assert not empty, "%s has empty entries: %s" % (
        os.path.basename(jar.path), empty[:10])


@pytest.mark.behavioural
@pytest.mark.parametrize("key", sorted(JARS))
def test_all_class_entries_are_class_files(jars, key):
    """Every *.class entry really is bytecode, not a copied source or stub."""
    jar = jars.require(key)
    bad = []
    for entry in jar.classes():
        if jar.read(entry)[:4] != b"\xca\xfe\xba\xbe":
            bad.append(entry)
    assert not bad, "%s has non-bytecode .class entries: %s" % (
        os.path.basename(jar.path), bad[:10])


@pytest.mark.audit
def test_gson_jar_has_no_test_classes(jars):
    """The published jar carries main classes only."""
    jar = jars.require("gson")
    leaked = [e for e in jar.classes()
              if e.endswith("Test.class") or "/test/" in e
              or e.endswith("TestCase.class")]
    assert not leaked, "test classes leaked into the gson jar: %s" % leaked[:10]


@pytest.mark.behavioural
@pytest.mark.parametrize("key", sorted(JARS))
def test_no_source_files_in_jar(jars, key):
    """No .java in a binary jar: the sources jar is a separate artifact."""
    jar = jars.require(key)
    srcs = [e for e in jar.entries if e.endswith(".java")]
    assert not srcs, "%s contains java sources: %s" % (
        os.path.basename(jar.path), srcs[:10])


@pytest.mark.behavioural
@pytest.mark.parametrize("key", sorted(JARS))
def test_no_build_artifacts_in_jar(jars, key):
    """No build-system residue: .gradle files, wrappers, POM templates."""
    jar = jars.require(key)
    bad = [e for e in jar.entries
           if e.endswith((".gradle", ".gradle.kts", "gradlew",
                          "gradle-wrapper.properties"))
           or e.startswith("META-INF/gradle/")]
    assert not bad, "%s contains build-system files: %s" % (
        os.path.basename(jar.path), bad[:10])


@pytest.mark.behavioural
@pytest.mark.parametrize("key", sorted(JARS))
def test_entries_are_deflated(jars, key):
    """A stored-not-deflated jar is a jar somebody assembled by hand.

    Both Maven's and Gradle's jar tasks deflate by default, and the manifest is
    the one entry either may store.  This is not a size check: it is the cheapest
    signal that the archive came out of a build rather than out of a script that
    zipped a directory.
    """
    jar = jars.require(key)
    stored = [e for e in jar.entries
              if jar.compress_type(e) == 0 and jar.size(e) > 0
              and e != "META-INF/MANIFEST.MF"]
    assert len(stored) < max(2, len(jar.entries) // 10), (
        "%s stores %d of %d entries uncompressed: %s"
        % (os.path.basename(jar.path), len(stored), len(jar.entries),
           stored[:10]))


@pytest.mark.behavioural
@pytest.mark.parametrize("key", sorted(JARS))
def test_no_nested_jars(jars, key):
    """None of the four artifacts is a shaded or fat jar.

    State A publishes four thin jars whose dependencies are declared, not
    bundled.  A build that produced a shaded jar would satisfy a classpath check
    and change what consumers resolve.

    The allowance comes from the reference build rather than from a rule: the
    metrics project ships `ParseBenchmarkData.zip` as an ordinary resource, and an
    archive the reference build packaged is delivered content.  What this asks is
    whether the submission bundled something the reference build did *not*.
    """
    jar = jars.require(key)
    shipped = {e for e in JARS[key]["required_entries"]
               if e.endswith((".jar", ".zip", ".so"))}
    nested = [e for e in jar.entries
              if e.endswith((".jar", ".zip", ".so")) and e not in shipped]
    assert not nested, "%s bundles archives: %s" % (
        os.path.basename(jar.path), nested[:10])


@pytest.mark.behavioural
def test_gson_jar_carries_no_dependency_classes(jars):
    """No package from outside the project leaked into the gson jar."""
    jar = jars.require("gson")
    foreign = sorted({e.rsplit("/", 1)[0] for e in jar.classes()
                      if not e.startswith("com/google/gson/")
                      and not e.startswith("META-INF/")})
    assert not foreign, (
        "the gson jar carries classes from outside com.google.gson: %s"
        % foreign[:10])
