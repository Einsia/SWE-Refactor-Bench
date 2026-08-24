#!/usr/bin/env python3
"""The build matrix: eight invocations, and the sources three of them move.

A build toolchain is not observable from one invocation.  The claims that matter
most here exist only as differences *between* invocations: that the version is a
property rather than a literal, that the jars are derived from the delivered
sources rather than enumerated, that a second run in the same tree produces the
same bytes, that nothing depends on where the tree sits.  The matrix is the
measuring instrument, not a convenience.

The `build` module runs everything here once and publishes a ledger; no other
module builds anything.  This file is therefore the single description of what
"the build" means for this task, and it is imported, never duplicated.

Why the probes are shaped the way they are
------------------------------------------
Every entry-set and per-entry checksum comparison in this suite -- 282 required
entries across four jars -- is satisfiable by a build that enumerates the names
it was told to produce.  So is every manifest header.  None of them says anything
about whether the build has *rules*.  The five files `m_add` writes did not exist
when the ground truth was frozen, and where each one has to land is decided by
the source-set layout and by bnd's `-exportcontents`, which names four packages
and not a wildcard:

    gson/src/main/java/com/google/gson/SrbProbeGson.java
        an exported package. Must reach the gson jar, and Export-Package must
        still list exactly four packages -- adding a class to one of them does
        not add a package.

    gson/src/main/java/com/google/gson/srbprobe/SrbProbeInternal.java
        a package that does not exist in State A and is not in -exportcontents.
        Must reach the jar, and must NOT appear in Export-Package. A build that
        computes its exports from "every package present" gets five.

    gson/src/main/resources/srb-probe.properties
        State A's gson module has no resource directory at all. A build that
        wires resources by convention picks this up; one that copies a known
        list of files does not.

    extras/.../SrbProbeExtras.java and metrics/.../SrbProbeMetrics.java
        per-project source sets. Each must reach its own jar and no other -- the
        gson jar in particular, since all three probes share a package prefix.

`m_edit` asks the same question of three things that are computed rather than
copied: the `${project.version}` filtering of the java-template, the compilation
of module-info.java into the multi-release directory, and the constant pool of a
class State A also ships.
"""

import builder
import dialect

# ------------------------------------------------------------------ constants --
PROBE_VERSION = "7.11.3"
PUBLISH_DIR = "srb-publish"
PUBLISH_PROPERTY = dialect.PUBLISH_PROP
VERSION_PROPERTY = dialect.VERSION_PROP

MARK_GSON = "SRB-PROBE-GSON-5E12"
MARK_PKG = "SRB-PROBE-PKG-A73C"
MARK_EXTRAS = "SRB-PROBE-EXTRAS-4B90"
MARK_METRICS = "SRB-PROBE-METRICS-C218"
MARK_RES = "SRB-PROBE-RES-71D4"
MARK_TEMPLATE = "SRB-PROBE-TEMPLATE-3F5A"
MARK_GSON_EDIT = "SRB-PROBE-EDIT-88E6"
MARK_TEST = "SRB-PROBE-TEST-D041"

# Probe classes must compile clean at release 7 under `-Xlint:all`: State A's
# build sets failOnWarning, a faithful port keeps it, and a probe that trips a
# warning would fail the build for a reason that has nothing to do with the
# migration. Hence: no generics, no serialization, no deprecated API, a private
# constructor and a javadoc comment on every public member.
_CLASS = '''\
package %(package)s;

/**
 * SWERefactorBench build probe. Written into the sources before this
 * configuration was built; where it lands is decided by the build's rules.
 */
public final class %(name)s {
  /** The marker this probe is recognised by. */
  public static final String MARK = "%(mark)s";

  private %(name)s() { }

  /**
   * Returns the marker.
   *
   * @return the marker
   */
  public static String mark() {
    return MARK;
  }
}
'''


def _cls(package, name, mark):
    return _CLASS % {"package": package, "name": name, "mark": mark}


PROBE_GSON_PATH = "gson/src/main/java/com/google/gson/SrbProbeGson.java"
PROBE_GSON_ENTRY = "com/google/gson/SrbProbeGson.class"
PROBE_PKG_PATH = ("gson/src/main/java/com/google/gson/srbprobe/"
                  "SrbProbeInternal.java")
PROBE_PKG_ENTRY = "com/google/gson/srbprobe/SrbProbeInternal.class"
PROBE_PKG_PACKAGE = "com.google.gson.srbprobe"
PROBE_EXTRAS_PATH = ("extras/src/main/java/com/google/gson/typeadapters/"
                     "SrbProbeExtras.java")
PROBE_EXTRAS_ENTRY = "com/google/gson/typeadapters/SrbProbeExtras.class"
PROBE_METRICS_PATH = ("metrics/src/main/java/com/google/gson/metrics/"
                      "SrbProbeMetrics.java")
PROBE_METRICS_ENTRY = "com/google/gson/metrics/SrbProbeMetrics.class"

# A resource, in a directory State A does not have. CRLF on purpose: a resource
# is copied, not rewritten, so it must arrive with the bytes it was written with.
PROBE_RES_PATH = "gson/src/main/resources/srb-probe.properties"
PROBE_RES_ENTRY = "srb-probe.properties"
PROBE_RES_BODY = ("# SWERefactorBench resource probe\r\n"
                  "srb.probe.mark=%s\r\n" % MARK_RES)

# A test in the project whose test task is the expensive one. Three cases, one of
# them ignored, so both the total and the skip count have to move.
PROBE_TEST_PATH = "gson/src/test/java/com/google/gson/SrbProbeGsonTest.java"
PROBE_TEST_CLASS = "com.google.gson.SrbProbeGsonTest"
PROBE_TEST_SRC = '''\
package com.google.gson;

import static org.junit.Assert.assertEquals;

import org.junit.Ignore;
import org.junit.Test;

/** SWERefactorBench test-source probe: three cases, one of them ignored. */
public class SrbProbeGsonTest {
  @Test
  public void probeSerialisesAnInteger() {
    assertEquals("41", new Gson().toJson(Integer.valueOf(41)));
  }

  @Test
  public void probeReadsTheMark() {
    assertEquals("%(mark)s", "%(mark)s");
  }

  @Ignore("SWERefactorBench probe: this case must be reported as skipped")
  @Test
  public void probeIsIgnored() {
    throw new AssertionError("this case must not run");
  }
}
''' % {"mark": MARK_TEST}

# ------------------------------------------------------------------- m_edit --
# The java-template. `${project.version}` is the token State A's
# templating-maven-plugin substitutes; the probe adds a *second* field carrying
# the same token, so a port that special-cases the one known line produces a
# class whose second constant still reads `${project.version}` literally.
TEMPLATE_PATH = ("gson/src/main/java-templates/com/google/gson/internal/"
                 "GsonBuildConfig.java")
TEMPLATE_OLD = '  public static final String VERSION = "${project.version}";'
TEMPLATE_NEW = (TEMPLATE_OLD + '\n\n'
                '  /** SWERefactorBench filtering probe. */\n'
                '  public static final String SRB_PROBE = "'
                + MARK_TEMPLATE + '-${project.version}";')
TEMPLATE_MARK_PREFIX = MARK_TEMPLATE + "-"

# module-info.java. State A exports four packages; the probe adds a fifth, which
# only reaches the artifact if the descriptor in META-INF/versions/9 was compiled
# from this file rather than carried over.
MODULE_PATH = "gson/src/main/java/module-info.java"
MODULE_OLD = "\texports com.google.gson.stream;"
MODULE_NEW = MODULE_OLD + "\n\texports com.google.gson.internal;"
MODULE_ADDED_EXPORT = "com.google.gson.internal"

# A class State A also ships. The constant has to appear in the constant pool of
# the packaged class file, which it does only if that file was compiled here.
GSON_PATH = "gson/src/main/java/com/google/gson/Gson.java"
GSON_OLD = "public final class Gson {"
GSON_NEW = ('public final class Gson {\n'
            '  /** SWERefactorBench derivation probe. */\n'
            '  public static final String SRB_PROBE = "%s";' % MARK_GSON_EDIT)
GSON_ENTRY = "com/google/gson/Gson.class"


def add_mutations():
    return [
        builder.Mutation("write", PROBE_GSON_PATH,
                         text=_cls("com.google.gson", "SrbProbeGson",
                                   MARK_GSON)),
        builder.Mutation("write", PROBE_PKG_PATH,
                         text=_cls(PROBE_PKG_PACKAGE, "SrbProbeInternal",
                                   MARK_PKG)),
        builder.Mutation("write", PROBE_EXTRAS_PATH,
                         text=_cls("com.google.gson.typeadapters",
                                   "SrbProbeExtras", MARK_EXTRAS)),
        builder.Mutation("write", PROBE_METRICS_PATH,
                         text=_cls("com.google.gson.metrics",
                                   "SrbProbeMetrics", MARK_METRICS)),
        builder.Mutation("write", PROBE_RES_PATH, text=PROBE_RES_BODY),
        builder.Mutation("write", PROBE_TEST_PATH, text=PROBE_TEST_SRC),
    ]


def edit_mutations():
    return [
        builder.Mutation("replace", TEMPLATE_PATH, old=TEMPLATE_OLD,
                         new=TEMPLATE_NEW),
        builder.Mutation("replace", MODULE_PATH, old=MODULE_OLD,
                         new=MODULE_NEW),
        builder.Mutation("replace", GSON_PATH, old=GSON_OLD, new=GSON_NEW),
    ]


# --------------------------------------------------------------- build matrix --
# Each configuration names an *intent* and `dialect` renders it into the argv of
# whichever build system the delivered tree declares -- `clean assemble` or
# `clean package -DskipTests`, `clean publish` or `clean deploy`.  What is being
# measured is what the invocation produced, so the intent is the durable part and
# the argv is not.
#
#   assemble   the four jars, without the test task. Every content module reads
#              this one, and that is deliberate -- a submission with one failing
#              test should still be paid for jars that are byte-correct, so the
#              jars are not read out of a configuration a test failure can abort.
#   full       assemble plus the test task. The `tests` module reads its JUnit
#              XML; this is the configuration that says the migrated build still
#              runs Gson's own 1328 cases.
#   publish    a publication into -PsrbPublishDir. What a consumer would get:
#              coordinates, the pom, the sources jar, and the three projects that
#              must NOT be published.
#   version    assemble and publish at -Pversion=7.11.3. Four jar names, a
#              filtered java-template, an OSGi Bundle-Version, a module version
#              and a published pom all have to move together. A build with 2.10.1
#              written into it passes every other configuration.
#   rebuild    assemble twice in one tree, with the first pass's jars copied
#              aside. Catches a build that is not idempotent and one whose clean
#              does not clean.
#   relocated  the same sources at a deeper absolute path. Catches a build file
#              that bakes in a path or depends on its own depth.
#   m_add      six sources and resources that did not exist when the ground truth
#              was frozen, then the test task so the new case runs too.
#   m_edit     three edits to things that are computed rather than copied: the
#              filtered template, the module descriptor, and a class body.
#
# `assemble` runs first: it is the tree the most modules read, so if the wall
# clock runs out the most valuable tree is the one already on disk.
ORDER = ("assemble", "full", "publish", "version", "rebuild", "relocated",
         "m_add", "m_edit")

# The two configurations that run the test task get the long timeout; `full`
# alone is 1328 cases across three projects.
TEST_TIMEOUT = 5400
PLAIN_TIMEOUT = 2400


def configurations():
    """Fresh kwargs per call: Mutation objects carry mutable `path`."""
    return {
        "assemble": dict(intent="assemble", timeout=PLAIN_TIMEOUT),
        "full": dict(intent="build", timeout=TEST_TIMEOUT),
        "publish": dict(intent="publish",
                        props={PUBLISH_PROPERTY: PUBLISH_DIR},
                        publish_dir=PUBLISH_DIR, timeout=PLAIN_TIMEOUT),
        # `publish` as well as `assemble`, because half of what the version feeds
        # is only visible in a publication: the repository path, the POM's own
        # <version>, and the file names beside it.  Running the two in one
        # configuration also asks something the pair asked separately cannot --
        # that the jar in the repository is the jar this build produced.
        "version": dict(intent="assemble+publish",
                        props={VERSION_PROPERTY: PROBE_VERSION,
                               PUBLISH_PROPERTY: PUBLISH_DIR},
                        publish_dir=PUBLISH_DIR,
                        timeout=PLAIN_TIMEOUT),
        "rebuild": dict(intent="assemble", second_intent="assemble",
                        snapshot_libs=True, timeout=TEST_TIMEOUT),
        "relocated": dict(intent="assemble",
                          subdir="a/deeper/place/gson-2.10.1",
                          timeout=PLAIN_TIMEOUT),
        "m_add": dict(intent="build", mutations=add_mutations(),
                      timeout=TEST_TIMEOUT),
        "m_edit": dict(intent="assemble", mutations=edit_mutations(),
                       timeout=PLAIN_TIMEOUT),
    }


def builds():
    """One unexecuted Build per configuration, in ORDER."""
    kw = configurations()
    return [builder.Build(name, **kw[name]) for name in ORDER]

