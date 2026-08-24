#!/usr/bin/env python3
"""What the built jars do when a program uses them.

Every other module in this suite reads the artefacts.  This one puts them on a
classpath, compiles five programs written against gson's public API, runs them,
and compares what they print against what the API documents.  It is the only place
the migration is measured by behaviour rather than by shape, and it is deliberately
blind to how the jars were produced: the probe sources name no build system, no
task, and no path inside the repository.

The expected values are gson's own contracts, not measurements of State A.
``toJson(new int[]{1,2,3})`` is ``[1,2,3]`` in every release, ``@SerializedName``
renames a field in every release, and a module whose descriptor is right exports
four packages in every release.  A probe that prints something else is reporting a
jar that does not behave like gson -- a stronger statement than "it differs from
the jar we captured", and one that survives the pinned upstream moving.

Five programs, all against the ``assemble`` configuration's output:

  gson     41 observations of the core API: adapters, generics, streaming, the
           exclusion strategies, pretty printing, number policies, records.
  module   what the JVM makes of the jar's module descriptor, with the jar on the
           module path.  A jar missing ``Multi-Release: true`` answers here as an
           automatic module named after the file; one with the descriptor at the
           jar root answers correctly here and breaks on Java 8; only the
           multi-release layout answers correctly in both.
  extras   RuntimeTypeAdapterFactory polymorphism and UtcDateTypeAdapter, from the
           extras jar against the gson jar.
  metrics  a benchmark model through Gson, plus the package-private runner the
           metrics jar exists to hold.
  proto    a real protobuf message round-tripped through ProtoTypeAdapter.
"""
import os

import pytest

from srbfixtures import probe_value

# --------------------------------------------------------------------- gson ---
GSON_EXPECT = [
    # arrays and collections
    ("int-array", "[1,2,3]"),
    ("nested-array", "[[1,2],[3]]"),
    ("string-list", '["a","b"]'),
    # nulls
    ("skip-null", '{"a":"x"}'),
    ("serialize-null", '{"a":"x","b":null}'),
    # insertion order is preserved, for both fields and LinkedHashMap keys
    ("field-order", '{"z":1,"a":2,"m":3}'),
    ("map-order", '{"z":1,"a":2,"m":3}'),
    # escaping: HTML-sensitive characters are escaped unless asked otherwise
    ("html-escape", '{"s":"\\u003cb\\u003e\\u0026\\u003d\\u0027"}'),
    ("no-html-escape", '{"s":"<b>&=\'"}'),
    # non-ASCII passes through as itself, and survives a round trip
    ("unicode-escaped", "false"),
    ("unicode-roundtrip", "65e5,672c"),
    # parsing, lenient and strict
    ("parse-int", "7"),
    ("parse-unquoted-name", "7"),
    ("empty-is-null", "null"),
    ("truncated-throws", "com.google.gson.JsonSyntaxException"),
    ("strict-throws", "com.google.gson.stream.MalformedJsonException"),
    # generics through TypeToken
    ("int-list", "3:java.lang.Integer"),
    ("nested-generic", "a=2"),
    # naming
    ("serialized-name", '{"my_key":"v"}'),
    ("alternate-name", "v"),
    ("naming-policy", '{"my_field":"v"}'),
    # pretty printing
    ("pretty", '{\\n  "a": 1\\n}'),
    # exclusion strategies
    ("expose-only", '{"a":"x"}'),
    ("since-version", '{"a":"x"}'),
    # the tree model
    ("jsonobject-tostring", '{"n":1}'),
    ("parse-tree", "5"),
    ("deep-copy", "1:2"),
    # the streaming API
    ("writer-out", '{"k":1}'),
    ("reader-tokens", "BEGIN_OBJECT,NAME,BEGIN_ARRAY,NUMBER,END_ARRAY,"
                      "END_OBJECT,END_DOCUMENT"),
    # custom adapters
    ("adapter-tojson", '"x"'),
    ("adapter-null", "null"),
    # numbers
    ("number-fields", '{"l":1,"d":1.5}'),
    ("object-number-class", "java.lang.Double"),
    ("bigdecimal", "1.10"),
    # enums
    ("enum-name", '{"e":"B"}'),
    ("enum-parse", "A"),
    # records, which is why gson's tests need release 17
    ("record-write", '{"a":1,"b":"x"}'),
    ("record-read", "2|y"),
    # the generated build config
    ("build-version", "2.10.1"),
]

#: With the jar on the class path rather than the module path, gson is in the
#: unnamed module.  Recorded so a submission that somehow forces a named module
#: onto a plain classpath is noticed.
GSON_CLASSPATH_EXPECT = [
    ("mode", "classpath"),
    ("module-name", "null"),
]

# ------------------------------------------------------------------- module ---
MODULE_EXPECT = [
    ("mode", "module"),
    ("module-name", "com.google.gson"),
    ("module-automatic", "false"),
    ("module-open", "false"),
    ("module-exports", "com.google.gson,com.google.gson.annotations,"
                       "com.google.gson.reflect,com.google.gson.stream"),
    ("module-requires", "java.base,java.sql,jdk.unsupported"),
    ("module-requires-static", "java.sql,jdk.unsupported"),
    ("module-packages", "9"),
    ("module-internal-packages", "com.google.gson.internal,"
                                 "com.google.gson.internal.bind,"
                                 "com.google.gson.internal.bind.util,"
                                 "com.google.gson.internal.reflect,"
                                 "com.google.gson.internal.sql"),
    ("exported-api", "[1,2,3]"),
    ("internal-access", "java.lang.IllegalAccessException"),
]

# ------------------------------------------------------------------- extras ---
EXTRAS_EXPECT = [
    ("extras-loaded", "com.google.gson.typeadapters.RuntimeTypeAdapterFactory"),
    ("runtime-type-write", '{"kind":"circle","r":1.5,"id":3}'),
    ("runtime-type-read", "Circle:4"),
    ("utc-date-write", '"1970-01-01T00:00:00.000Z"'),
    ("utc-date-read", "1000"),
]

# ------------------------------------------------------------------ metrics ---
METRICS_EXPECT = [
    ("metrics-loaded", "com.google.gson.metrics.BagOfPrimitives"),
    ("metrics-json", '{"longValue":10,"intValue":20,"booleanValue":false,'
                     '"stringValue":"stringValue"}'),
    ("metrics-runner", "com.google.gson.metrics.NonUploadingCaliperRunner"),
]

# -------------------------------------------------------------------- proto ---
PROTO_EXPECT = [
    ("proto-loaded", "com.google.gson.protobuf.ProtoTypeAdapter"),
    ("proto-adapter", "ProtoTypeAdapter"),
    ("proto-name", "field_one"),
    ("proto-number", "7"),
    # TYPE_STRING is wire value 9, and NUMBER serialisation was asked for.
    ("proto-enum-number", "9"),
    ("proto-roundtrip", "field_one|7|TYPE_STRING"),
    ("proto-equal", "true"),
]

ALL = ([("gson", k, v) for k, v in GSON_EXPECT + GSON_CLASSPATH_EXPECT] +
       [("module", k, v) for k, v in MODULE_EXPECT] +
       [("extras", k, v) for k, v in EXTRAS_EXPECT] +
       [("metrics", k, v) for k, v in METRICS_EXPECT] +
       [("proto", k, v) for k, v in PROTO_EXPECT])

PROBES = ("gson", "module", "extras", "metrics", "proto")


# ------------------------------------------------------- the probes compiled ---
@pytest.mark.audit
@pytest.mark.parametrize("probe", PROBES)
def test_probe_compiled(runtime, probe):
    """A program written against the public API compiles against this jar.

    The check a submission with a missing class, or with classes renamed by an
    over-eager obfuscation step, fails first: javac resolves every reference the
    probe makes, so one absent public type stops it here with the compiler's own
    message attached.
    """
    p = runtime[probe]
    assert p.compiled, (
        "the %s probe did not compile against the delivered jars.\n\n%s"
        % (probe, p.failure_summary()))


@pytest.mark.audit
@pytest.mark.parametrize("probe", PROBES)
def test_probe_ran_to_completion(runtime, probe):
    """It also runs, and reaches its last line.

    Compiling proves the API surface is present; running proves the class files
    behind it link and work.  A jar assembled from classes compiled at a release
    the JVM will not load fails here rather than above.
    """
    p = runtime[probe]
    assert p.complete, (
        "the %s probe did not run to completion.\n\n%s"
        % (probe, p.failure_summary()))


# --------------------------------------------------------------- observations ---
@pytest.mark.behavioural
@pytest.mark.parametrize("probe,key,want", ALL,
                         ids=["%s/%s" % (p, k) for p, k, _ in ALL])
def test_observation(runtime, probe, key, want):
    """One observation matches what gson's API documents."""
    got = probe_value(runtime, probe, key)
    assert got == want, (
        "%s probe, %s:\n  expected: %s\n  observed: %s" % (probe, key, want, got))


# --------------------------------------------------------- shape of the answers ---
@pytest.mark.behavioural
def test_module_requires_nothing_mandatory_beyond_java_base(runtime):
    """Everything gson requires except java.base is optional.

    ``java.sql`` and ``jdk.unsupported`` are declared static, so a runtime image
    without them still resolves the module.  A descriptor that drops the modifier
    makes gson unusable on exactly the stripped images its module descriptor
    exists to serve.
    """
    requires = [r for r in probe_value(runtime, "module",
                                       "module-requires").split(",") if r]
    static = {r for r in probe_value(runtime, "module",
                                     "module-requires-static").split(",") if r}
    mandatory = [r for r in requires if r != "java.base" and r not in static]
    assert not mandatory, (
        "the module requires %s without `static`; State A declares every "
        "requirement but java.base as static" % mandatory)


@pytest.mark.behavioural
def test_module_package_count_covers_exports_and_internals(runtime):
    """The descriptor knows about the packages it does not export.

    A descriptor generated from the exported packages alone counts four instead
    of nine, and the module then cannot see its own internals.
    """
    total = int(probe_value(runtime, "module", "module-packages"))
    exports = [p for p in probe_value(runtime, "module",
                                      "module-exports").split(",") if p]
    internal = [p for p in probe_value(runtime, "module",
                                       "module-internal-packages").split(",") if p]
    assert total == len(exports) + len(internal), (
        "the descriptor lists %d package(s); it exports %d and has %d internal "
        "ones" % (total, len(exports), len(internal)))


@pytest.mark.behavioural
def test_internal_packages_are_not_readable_from_outside(runtime):
    """Reflection into com.google.gson.internal is refused.

    The complement of ``exported-api``: the jar is only a real module if the
    encapsulation it declares is enforced.  A jar that answers ``readable:...``
    here has either exported its internals or arrived as an automatic module,
    which opens everything.
    """
    got = probe_value(runtime, "module", "internal-access")
    assert not got.startswith("readable"), (
        "com.google.gson.internal is reflectively readable from outside the "
        "module (%s); State A encapsulates it" % got)


@pytest.mark.behavioural
def test_no_probe_printed_an_error_line(runtime):
    """No observation was produced by an exception being swallowed.

    Each probe prints ``<name>.error`` when a step throws, so a partial answer is
    distinguishable from a wrong one.  A subtly incomplete jar typically produces
    a handful of these rather than failing outright.
    """
    errors = {}
    for probe in PROBES:
        bad = {k: v for k, v in runtime[probe].values.items()
               if k.endswith(".error")}
        if bad:
            errors[probe] = bad
    assert not errors, (
        "probe step(s) threw:\n%s"
        % "\n".join("%s %s: %s" % (p, k, v)
                    for p, kv in sorted(errors.items())
                    for k, v in sorted(kv.items())))


@pytest.mark.behavioural
@pytest.mark.parametrize("probe", PROBES)
def test_probe_answered_every_observation(runtime, probe):
    """This probe's observation set is complete.

    Stated per probe alongside the per-observation checks so that a program which
    printed nothing at all reports as one missing set rather than as forty
    unrelated failures.
    """
    want = {k for p, k, _ in ALL if p == probe}
    missing = sorted(want - set(runtime[probe].values))
    assert not missing, (
        "the %s probe printed %d of %d observations; missing %s\n\n%s"
        % (probe, len(want) - len(missing), len(want), missing[:8],
           runtime[probe].failure_summary()))


@pytest.mark.audit
@pytest.mark.parametrize("probe", PROBES)
def test_probe_used_the_delivered_jars(runtime, b_assemble, probe):
    """This probe's classpath is the build's own output.

    Guards the module against the failure that would make it meaningless: if a
    gson jar from the image's offline dependency cache reached a classpath, every
    observation above would pass while measuring nothing the submission built.
    """
    delivered = {os.path.realpath(p)
                 for p in b_assemble.present_jars().values()}
    entries = [os.path.realpath(c)
               for c in list(runtime[probe].classpath) +
               list(runtime[probe].module_path)]
    gson_like = [c for c in entries
                 if os.path.basename(c).startswith("gson")]
    assert gson_like, "the %s probe had no gson jar on its classpath" % probe
    outside = [c for c in gson_like if c not in delivered]
    assert not outside, (
        "the %s probe ran against a gson jar the build did not produce: %s"
        % (probe, outside))
