#!/usr/bin/env python3
"""The four artefacts State A derives rather than copies.

A migration that only moves compilation across still produces four jars, and
three of these four leave no trace in them, so this is where a build that skipped
a generation step is separated from one that reproduced it:

  GsonBuildConfig.java
      ``gson/src/main/java-templates/`` holds it with ``${project.version}`` in
      the source.  Maven's templating-maven-plugin filters the token into
      ``target/generated-sources`` and compiles the result.  The class ends up in
      the jar, so the substitution *is* observable there -- and the token surviving
      into a class file is the failure mode a build that merely copied the
      directory produces.

  META-INF/versions/9/module-info.class
      ``gson/src/main/java/module-info.java`` compiles at release 9 while
      everything beside it compiles at release 7.  moditect puts the result under
      ``versions/9/`` so the jar keeps working on Java 8.

  the obfuscated test classes
      proguard runs over gson's compiled test classes and
      ``EnumWithObfuscatedTest`` asserts that its own enum's fields are no longer
      reflectively findable.  Only the build tree shows whether the step ran.

  the protobuf messages
      ``proto/src/test/proto/*.proto`` are compiled by protoc into
      ``generated-test-sources``; the proto tests import the result.  Nothing about
      the proto jar reveals whether they were generated or vendored.

Every check reads either a jar or ``<project>/build/``.  None reads a build
script: what a submission called its tasks, and which plugin it reached for, is
not what is being measured.
"""
import json
import os
import re

import pytest

import classfile

DATA = os.path.join(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"), "data")
with open(os.path.join(DATA, "sources.json")) as _fh:
    SOURCES = json.load(_fh)
with open(os.path.join(DATA, "build_contract.json")) as _fh:
    CONTRACT = json.load(_fh)

VERSION = CONTRACT["version"]
BUILD_CONFIG = "com/google/gson/internal/GsonBuildConfig.class"
DESCRIPTOR = "META-INF/versions/9/module-info.class"

#: The three protobuf message classes protoc generates from bag.proto and
#: annotations.proto, named as the test sources import them.
PROTO_GENERATED = [
    "com/google/gson/protobuf/generated/Bag.class",
    "com/google/gson/protobuf/generated/Annotations.class",
]

#: Every nested message class the proto tests actually touch.  Each one is a
#: separate check because protoc emits them from one .proto file and a
#: hand-vendored subset is the thing this distinguishes.
PROTO_NESTED = [
    "com/google/gson/protobuf/generated/Bag$SimpleProto.class",
    "com/google/gson/protobuf/generated/Bag$ProtoWithAnnotations.class",
    "com/google/gson/protobuf/generated/Bag$ProtoWithRepeatedFields.class",
    "com/google/gson/protobuf/generated/Bag$OuterMessage.class",
]


def _classes_under(build, project, pattern="*.class"):
    return build.outputs(project, pattern)


# ------------------------------------------------------ the filtered template --
@pytest.mark.audit
def test_build_config_class_is_in_the_jar(gson_jar):
    """GsonBuildConfig reaches the jar at all.

    It is generated, so a build that never wired the template directory into a
    source set compiles everything else successfully and simply lacks this class
    -- and then fails at runtime the first time anything reads
    ``GsonBuildConfig.VERSION``.
    """
    assert gson_jar.has(BUILD_CONFIG), (
        "%s is missing from the jar.\nIt is generated from "
        "gson/src/main/java-templates/ by filtering ${project.version} into the "
        "source; that directory has to become a generated source set."
        % BUILD_CONFIG)


@pytest.mark.behavioural
def test_build_config_carries_the_project_version(gson_jar):
    """The constant reads 2.10.1, from the build, not from the template."""
    cf = classfile.ClassFile(gson_jar.read(BUILD_CONFIG))
    strings = set(cf.strings())
    assert VERSION in strings, (
        "GsonBuildConfig does not contain the string %r.\nConstant pool "
        "strings: %s" % (VERSION, sorted(s for s in strings if s and len(s) < 40)))


@pytest.mark.behavioural
def test_build_config_has_no_unsubstituted_token(gson_jar):
    """No ``${...}`` survives into the class file.

    A source set that includes ``java-templates`` without filtering it compiles
    happily: ``"${project.version}"`` is a perfectly good string literal.  The
    class then reports its version as the token, and every consumer that compares
    versions is quietly wrong.
    """
    cf = classfile.ClassFile(gson_jar.read(BUILD_CONFIG))
    leftover = [s for s in cf.strings() if "${" in s]
    assert not leftover, (
        "GsonBuildConfig still contains unfiltered token(s) %r -- the template "
        "was compiled without substituting the version into it" % leftover)


@pytest.mark.behavioural
def test_build_config_declares_the_version_constant(gson_jar):
    """The field is still called VERSION and is still a constant.

    Behavioural, not cosmetic: it is a public compile-time constant that
    downstream code inlines, so a build that turned it into a computed field
    changes what consumers see.
    """
    cf = classfile.ClassFile(gson_jar.read(BUILD_CONFIG))
    names = set(cf.field_names())
    assert "VERSION" in names, (
        "GsonBuildConfig declares no VERSION field; it declares %s"
        % sorted(names))


@pytest.mark.behavioural
def test_template_directory_is_not_shipped_as_a_resource(gson_jar):
    """The unfiltered template is not in the jar beside its filtered class.

    A build that adds ``java-templates`` as a *resource* directory gets the
    substitution wrong in the least visible way: the class file is fine, because
    the real source is elsewhere, and the raw ``.java`` rides along in the jar.
    """
    stowaways = [e for e in gson_jar.entries
                 if "java-templates" in e or e.endswith("GsonBuildConfig.java")]
    assert not stowaways, (
        "the jar carries template source: %s" % stowaways)


@pytest.mark.behavioural
def test_generated_source_is_not_written_into_the_source_tree(b_assemble):
    """Generation lands in the build directory, not next to the template.

    Writing the filtered copy into ``gson/src/main/java`` makes the second build
    compile two ``GsonBuildConfig`` classes and makes ``git status`` dirty after a
    build.  The generated file must live under ``build/``.
    """
    stray = os.path.join(b_assemble.root, "gson", "src", "main", "java",
                         "com", "google", "gson", "internal",
                         "GsonBuildConfig.java")
    assert not os.path.exists(stray), (
        "the build wrote its generated GsonBuildConfig.java into "
        "gson/src/main/java -- generated sources belong under build/")


@pytest.mark.behavioural
def test_generated_source_exists_in_the_build_tree(b_assemble):
    """Some generated GsonBuildConfig.java is under gson/build.

    This is the positive half of the check above: the substitution happened
    somewhere the build owns.  Where exactly is the submission's business.
    """
    hits = b_assemble.outputs("gson", "*GsonBuildConfig.java")
    assert hits, (
        "no generated GsonBuildConfig.java anywhere under gson/build -- the "
        "template was either compiled in place or not processed at all")


@pytest.mark.behavioural
def test_generated_source_was_filtered(b_assemble):
    """The generated copy has the version, not the token."""
    hits = b_assemble.outputs("gson", "*GsonBuildConfig.java")
    if not hits:
        pytest.fail("no generated GsonBuildConfig.java under gson/build")
    text = open(hits[0], errors="replace").read()
    assert "${" not in text, (
        "%s still contains an unsubstituted token"
        % os.path.relpath(hits[0], b_assemble.root))
    assert VERSION in text, (
        "%s does not mention %s" % (os.path.relpath(hits[0], b_assemble.root),
                                    VERSION))


# ------------------------------------------------------- the module descriptor --
@pytest.mark.audit
def test_module_descriptor_was_compiled(gson_jar):
    """``META-INF/versions/9/module-info.class`` exists.

    Not the source, the compiled descriptor.  It needs its own compilation at
    release 9 in a project whose main output is release 7, which is the piece a
    single ``java { release = 7 }`` cannot express.
    """
    assert gson_jar.has(DESCRIPTOR), (
        "the jar has no %s.\ngson/src/main/java/module-info.java must be "
        "compiled at release 9 and placed under META-INF/versions/9/." % DESCRIPTOR)


@pytest.mark.behavioural
def test_module_descriptor_source_is_not_compiled_with_the_main_classes(gson_jar):
    """``module-info.class`` is not in the jar root.

    Putting it there is what happens when module-info.java is left in the main
    source set: the jar then requires Java 9 to *read*, and gson supports 8.
    """
    assert not gson_jar.has("module-info.class"), (
        "module-info.class is at the jar root. State A keeps it under "
        "META-INF/versions/9/ so the jar stays readable on Java 8.")


@pytest.mark.behavioural
def test_module_descriptor_source_is_not_in_the_jar(gson_jar):
    src = [e for e in gson_jar.entries if e.endswith("module-info.java")]
    assert not src, "module-info.java is shipped in the jar: %s" % src


@pytest.mark.behavioural
def test_main_classes_did_not_move_under_versions(gson_jar):
    """Only the descriptor is multi-released.

    A build that compiles the whole main source set twice produces a jar that
    passes every entry check and doubles in size, with a release-9 copy of every
    class shadowing the release-7 one on any modern JVM.
    """
    under = [e for e in gson_jar.entries
             if e.startswith("META-INF/versions/") and e != DESCRIPTOR]
    assert not under, (
        "%d entr(y/ies) besides the descriptor live under META-INF/versions/: %s"
        % (len(under), under[:8]))


# ------------------------------------------------------------- the obfuscation --
@pytest.mark.audit
def test_obfuscated_test_classes_were_produced(b_full):
    """proguard ran over gson's test classes.

    ``EnumWithObfuscatedTest`` asserts that its own enum's fields cannot be found
    reflectively, i.e. that it is running against an obfuscated copy of itself.
    Without the step the test fails with "Enum is not obfuscated" -- but the test
    failure alone does not say whether the step is missing or broken, so the
    artefact is checked directly.
    """
    hits = b_full.outputs("gson", "*EnumWithObfuscated*")
    assert hits, (
        "no obfuscated test output under gson/build.\nState A runs proguard over "
        "the compiled test classes using gson/src/test/resources/"
        "testcases-proguard.conf and runs the tests against the result.")


# Whether the step *did* something -- as opposed to running with a configuration
# that keeps every name -- is measured by `EnumWithObfuscatedTest` in the `tests`
# module, which passes only if the renaming actually happened and is not skippable.
# Corroborating that here by diffing the plain and obfuscated copies of the class on
# disk would need both to be found, and where the intermediate copies land, and
# whether both survive the build, is a layout decision this suite does not get to
# dictate: a submission that obfuscates in place, or into a directory such a check
# did not look in, has done nothing wrong.  A skip is charged 0 rather than leaving
# the denominator, so it would cost every submission the same check for a question
# none of them was asked, to corroborate a measurement that is already unskippable.


@pytest.mark.behavioural
def test_obfuscation_did_not_touch_the_main_classes(gson_jar, data):
    """The published jar is not obfuscated.

    proguard is aimed at the *test* classes.  Pointing it at the main classes
    instead produces a jar whose public API is renamed, which every downstream
    consumer notices and no test in this suite would otherwise ask about.
    """
    classes = data["classes"]["gson"]["exact"]
    names = set(gson_jar.classes())
    missing = sorted(set(classes) - names)
    assert not missing, (
        "%d class(es) State A publishes are absent from the jar: %s\n"
        "If the names look mangled, proguard was pointed at the main source set."
        % (len(missing), missing[:8]))


@pytest.mark.behavioural
def test_proguard_mapping_is_not_shipped(gson_jar):
    """proguard's map and seed files stay in the build directory."""
    leaked = [e for e in gson_jar.entries
              if "proguard" in e.lower() or e.endswith(("_map.txt", "_seed.txt"))]
    assert not leaked, "proguard bookkeeping is in the jar: %s" % leaked


# ----------------------------------------------------------- protobuf messages --
@pytest.mark.audit
def test_protobuf_messages_were_generated(b_full):
    """protoc ran over proto/src/test/proto.

    The generated classes are test-only, so they appear in no jar; the only place
    they can be observed is the proto project's build tree.
    """
    hits = b_full.outputs("proto", "*generated/Bag*.class")
    assert hits, (
        "no generated protobuf classes under proto/build.\n"
        "proto/src/test/proto/bag.proto and annotations.proto must be compiled "
        "by protoc; the proto tests import "
        "com.google.gson.protobuf.generated.Bag.")


@pytest.mark.behavioural
@pytest.mark.parametrize("entry", PROTO_GENERATED,
                         ids=[e.rsplit("/", 1)[-1] for e in PROTO_GENERATED])
def test_generated_message_class_present(b_full, entry):
    """This top-level generated message exists."""
    name = entry.rsplit("/", 1)[-1]
    hits = b_full.outputs("proto", "*" + name)
    assert hits, "%s was not generated under proto/build" % entry


@pytest.mark.behavioural
@pytest.mark.parametrize("entry", PROTO_NESTED,
                         ids=[e.rsplit("/", 1)[-1] for e in PROTO_NESTED])
def test_generated_nested_message_present(b_full, entry):
    """This nested message exists.

    protoc emits all of them from one file; a vendored subset is what this
    catches.
    """
    name = entry.rsplit("/", 1)[-1].replace("$", "?")
    hits = b_full.outputs("proto", "*" + name)
    assert hits, "%s was not generated under proto/build" % entry


@pytest.mark.behavioural
def test_generated_java_is_not_written_into_the_source_tree(b_full):
    """The generated messages do not land in proto/src.

    Same failure as the template: a build that generates into its own sources is
    not reproducible from a clean checkout twice.
    """
    stray = os.path.join(b_full.root, "proto", "src", "test", "java",
                         "com", "google", "gson", "protobuf", "generated")
    assert not os.path.isdir(stray), (
        "protoc wrote into proto/src/test/java/.../generated -- generated "
        "sources belong under build/")


@pytest.mark.behavioural
def test_generated_messages_are_not_shipped_in_the_proto_jar(jars, data):
    """The proto jar publishes the adapter, not the test messages."""
    jar = jars.require("gson-proto")
    leaked = [e for e in jar.entries if "/generated/" in e]
    assert not leaked, (
        "%s ships generated test messages: %s"
        % (os.path.basename(jar.path), leaked[:6]))


@pytest.mark.behavioural
def test_proto_files_are_not_shipped(jars):
    """No ``.proto`` rides along in any jar."""
    bad = {}
    for key in ("gson", "gson-extras", "gson-metrics", "gson-proto"):
        jar = jars.require(key)
        hits = [e for e in jar.entries if e.endswith(".proto")]
        if hits:
            bad[key] = hits
    assert not bad, ".proto files are shipped: %r" % bad


# --------------------------------------------------- derivation is repeatable --
@pytest.mark.behavioural
def test_second_build_regenerates_the_same_build_config(jars, rebuild_jars):
    """The filtered template is identical after clean and rebuild.

    Generation steps are where a build most easily picks up a timestamp: a
    template filtered with ``${build.date}`` alongside the version, or a protoc
    invocation that records when it ran.
    """
    a = jars.require("gson").read(BUILD_CONFIG)
    b = rebuild_jars.require("gson").read(BUILD_CONFIG)
    assert a == b, (
        "GsonBuildConfig.class differs between two builds of the same sources; "
        "something time- or environment-dependent is being substituted into it")


@pytest.mark.behavioural
def test_second_build_regenerates_the_same_descriptor(jars, rebuild_jars):
    a = jars.require("gson").read(DESCRIPTOR)
    b = rebuild_jars.require("gson").read(DESCRIPTOR)
    assert a == b, "the module descriptor differs between two builds"


@pytest.mark.behavioural
def test_derived_class_targets_match_their_project(gson_jar, data):
    """The generated class is compiled like the rest of the project.

    GsonBuildConfig is generated, and a build that compiles generated sources in a
    separate task easily gives that task a different release level -- producing
    one class in the jar that will not load on Java 7 while the other 218 will.
    """
    want = data["classes"]["gson"]["exact"][BUILD_CONFIG]["major"]
    got = gson_jar.major(BUILD_CONFIG)
    assert got == want, (
        "GsonBuildConfig.class targets major %d, the rest of the jar targets %d"
        % (got, want))


@pytest.mark.behavioural
def test_descriptor_targets_release_nine(gson_jar, data):
    """The descriptor is major 53; anything else is not a Java 9 descriptor."""
    want = data["module_info"]["major"]
    got = gson_jar.major(DESCRIPTOR)
    assert got == want, (
        "%s targets major %d, State A targets %d (release 9)"
        % (DESCRIPTOR, got, want))
