#!/usr/bin/env python3
"""Run the jars the submission built, and report what Gson actually did.

This is the part of the suite that does not read an artifact -- it uses one.  A
jar can have all 219 of the right entry names, the right per-entry checksums and a
byte-identical manifest and still not work: a `-Xlint`-clean compile at the wrong
release, a module descriptor compiled against the wrong classes, a resource that
arrived with the wrong bytes.  So a small consumer program is compiled *against*
the delivered jar and run, and every line it prints is one observation.

Why the expected values live in the pytest module rather than in data/
---------------------------------------------------------------------
Because they are not measurements of State A, they are Gson's documented
behaviour.  `new Gson().toJson(new int[] {1, 2, 3})` is `[1,2,3]` in every version
of the library, and writing that in the check where a reader can see it is more
honest than freezing it into a JSON file that looks like evidence.  Nothing here
was captured from a build; the probe program asks questions whose answers were
decided by Gson's API contract years ago.

The consumer is compiled with the image's javac and run with the image's java,
with the stub directory still first on PATH -- so a probe that somehow reached for
`mvn` would fail the same way a build would.
"""
from __future__ import annotations

import os
import re
import subprocess

import builder

SEP = "\t"
OK_LINE = "SRB-RUNTIME-COMPLETE"


def m2_jar(group, artifact, version=None):
    """A dependency jar from the image's offline repository, or None.

    Used to put protobuf on the classpath for the proto probe.  The version is
    optional: what matters is that the probe has *a* protobuf to compile against,
    not which one, since the claim being tested is about the delivered jar.
    """
    root = os.path.join(os.environ.get("HOME", "/root"), ".m2", "repository")
    base = os.path.join(root, *group.split("."), artifact)
    if not os.path.isdir(base):
        return None
    versions = sorted(v for v in os.listdir(base)
                      if os.path.isdir(os.path.join(base, v)))
    if version and version in versions:
        versions = [version]
    for v in reversed(versions):
        p = os.path.join(base, v, "%s-%s.jar" % (artifact, v))
        if os.path.isfile(p):
            return p
    return None


class Probe:
    """Compile one consumer program against a classpath, run it, read its lines.

    Neither failure raises.  A probe that does not compile and a probe that
    crashes are both legitimate outcomes of a broken migration, and the checks
    that wanted a value need to report the compiler's own words rather than a
    Python traceback about a missing dictionary key.
    """

    def __init__(self, work, name, source, classpath, module_path=None,
                 java_args=(), main_class=None, timeout=600, run_classpath=None):
        self.work = work
        self.name = name
        self.source = source
        self.classpath = [c for c in classpath if c]
        # A jar on the module path must not also be on the class path, or the
        # same packages arrive twice and the named module never forms.  So the
        # run classpath is separable from the compile classpath.
        self.run_classpath = ([c for c in run_classpath if c]
                              if run_classpath is not None else list(self.classpath))
        self.module_path = [c for c in (module_path or []) if c]
        self.java_args = list(java_args)
        self.main_class = main_class or name
        self.timeout = timeout
        self.compiled = False
        self.ran = False
        self.rc = None
        self.compile_log = ""
        self.run_log = ""
        self.values = {}

    @property
    def dir(self):
        return os.path.join(self.work, self.name)

    def _exec(self, cmd, cwd):
        try:
            p = subprocess.run(cmd, cwd=cwd, env=builder.clean_env(),
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=self.timeout)
            return p.returncode, p.stdout.decode("utf-8", "replace")
        except subprocess.TimeoutExpired as exc:
            return 124, (exc.stdout or b"").decode("utf-8", "replace") + \
                "\n*** TIMEOUT after %ss ***\n" % self.timeout
        except OSError as exc:
            return 127, "could not run %s: %r" % (cmd[0], exc)

    def run(self):
        os.makedirs(self.dir, exist_ok=True)
        src = os.path.join(self.dir, self.main_class + ".java")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(self.source)
        classes = os.path.join(self.dir, "classes")
        os.makedirs(classes, exist_ok=True)
        cp = os.pathsep.join(self.classpath) or "."
        rc, log = self._exec(["javac", "-nowarn", "-cp", cp, "-d", classes, src],
                             self.dir)
        self.compile_log = log
        self.compiled = rc == 0
        if not self.compiled:
            return self
        cmd = ["java"]
        if self.module_path:
            cmd += ["--module-path", os.pathsep.join(self.module_path)]
        cmd += self.java_args
        cmd += ["-cp", os.pathsep.join(self.run_classpath + [classes]),
                self.main_class]
        self.rc, self.run_log = self._exec(cmd, self.dir)
        self.ran = True
        for line in self.run_log.splitlines():
            if SEP in line and not line.startswith(("\t", "Note:", "WARNING")):
                key, _, value = line.partition(SEP)
                if re.fullmatch(r"[a-z0-9][a-z0-9.\-]*", key):
                    self.values[key] = value
        return self

    @property
    def complete(self):
        return self.ran and OK_LINE in self.run_log

    def get(self, key):
        return self.values.get(key)

    def failure_summary(self, limit=2500):
        if not self.compiled:
            head = ("the %s probe did not compile against the delivered jars; "
                    "javac said:" % self.name)
            body = self.compile_log
        elif not self.complete:
            head = ("the %s probe compiled but did not run to completion "
                    "(rc=%s); its output was:" % (self.name, self.rc))
            body = self.run_log
        else:
            head = "the %s probe ran; its output was:" % self.name
            body = self.run_log
        return "%s\n%s" % (head, body.strip()[-limit:])


PROBES = ("gson", "module", "extras", "metrics", "proto")


def run_all(build, work=None):
    """Every probe against one built tree, as a dict of name -> Probe.

    A probe whose own jar is missing is still returned, un-run, so a check can
    say "there was no gson-extras jar to use" rather than raising a KeyError two
    modules later.  The gson jar is the one dependency every probe needs; if it
    is missing, all five report that and nothing is compiled.
    """
    work = work or os.path.join(builder.WORK, "runtime")
    os.makedirs(work, exist_ok=True)
    gson = build.primary_jar("gson")
    jars = {p: build.primary_jar(p) for p in ("extras", "metrics", "proto")}
    # ProtoTypeAdapter names guava types in its own signatures, so guava has to
    # be reachable for the class to link even though the probe never calls it.
    protobuf = m2_jar("com.google.protobuf", "protobuf-java")
    guava = m2_jar("com.google.guava", "guava")
    caliper = m2_jar("com.google.caliper", "caliper")
    jsr250 = m2_jar("javax.annotation", "jsr250-api")

    specs = {
        "gson": dict(source=GSON_SOURCE, main_class="GsonProbe",
                     classpath=[gson]),
        # The module probe compiles against the jar as an ordinary library and
        # runs with it on the module path only -- the same jar cannot be in both
        # places without the named module dissolving into split packages.
        "module": dict(source=MODULE_SOURCE, main_class="ModuleProbe",
                       classpath=[gson], run_classpath=[],
                       module_path=[gson],
                       java_args=["--add-modules", "com.google.gson"]),
        "extras": dict(source=EXTRAS_SOURCE, main_class="ExtrasProbe",
                       classpath=[gson, jars["extras"], jsr250],
                       needs="extras"),
        "metrics": dict(source=METRICS_SOURCE, main_class="MetricsProbe",
                        classpath=[gson, jars["metrics"], caliper],
                        needs="metrics"),
        "proto": dict(source=PROTO_SOURCE, main_class="ProtoProbe",
                      classpath=[gson, jars["proto"], protobuf, guava],
                      needs="proto"),
    }

    out = {}
    for name in PROBES:
        spec = dict(specs[name])
        needs = spec.pop("needs", None)
        probe = Probe(work, name, **spec)
        out[name] = probe
        if not gson:
            probe.compile_log = ("there was no gson jar in the assembled tree, "
                                 "so nothing could be compiled against it")
            continue
        if needs and not jars[needs]:
            probe.compile_log = ("there was no gson-%s jar in the assembled "
                                 "tree, so nothing could be compiled against it"
                                 % needs)
            continue
        probe.run()
    return out


GSON_SOURCE = r'''
import com.google.gson.FieldNamingPolicy;
import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.google.gson.TypeAdapter;
import com.google.gson.annotations.Expose;
import com.google.gson.annotations.SerializedName;
import com.google.gson.annotations.Since;
import com.google.gson.reflect.TypeToken;
import com.google.gson.stream.JsonReader;
import com.google.gson.stream.JsonWriter;
import java.io.StringReader;
import java.io.StringWriter;
import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Asks the delivered gson jar what it does, one tab-separated line per answer. */
public final class GsonProbe {

  private interface Step { void run() throws Exception; }

  private static void step(String name, Step s) {
    try {
      s.run();
    } catch (Throwable t) {
      p(name + ".error", t.getClass().getName() + ": " + t.getMessage());
    }
  }

  /** Prints one observation, escaped so the line is always ASCII and single-line. */
  private static void p(String key, String value) {
    StringBuilder sb = new StringBuilder(key).append('\t');
    for (int i = 0; i < value.length(); i++) {
      char c = value.charAt(i);
      if (c == '\n') {
        sb.append("\\n");
      } else if (c == '\r') {
        sb.append("\\r");
      } else if (c == '\t') {
        sb.append("\\t");
      } else if (c < 0x20 || c > 0x7e) {
        sb.append("<u+").append(Integer.toHexString(c)).append('>');
      } else {
        sb.append(c);
      }
    }
    System.out.println(sb);
  }

  static class Bag {
    String a = "x";
    String b = null;
  }

  static class Ordered {
    int z = 1;
    int a = 2;
    int m = 3;
  }

  static class Str {
    String s;
    Str(String s) { this.s = s; }
  }

  static class Named {
    @SerializedName("my_key")
    String k = "v";
  }

  static class Alt {
    @SerializedName(value = "primary", alternate = {"alt"})
    String k;
  }

  static class Camel {
    String myField = "v";
  }

  static class Exposed {
    @Expose
    String a = "x";
    String b = "y";
  }

  static class Versioned {
    String a = "x";
    @Since(2.0)
    String b = "y";
  }

  static class Numbers {
    long l = 1L;
    double d = 1.5d;
  }

  enum Letter { A, B }

  static class Holder {
    Letter e = Letter.B;
  }

  record Pair(int a, String b) { }

  public static void main(String[] args) {
    p("mode", Gson.class.getModule().isNamed() ? "module" : "classpath");
    p("module-name", String.valueOf(Gson.class.getModule().getName()));

    step("arrays", () -> {
      Gson g = new Gson();
      p("int-array", g.toJson(new int[] {1, 2, 3}));
      p("nested-array", g.toJson(new int[][] {{1, 2}, {3}}));
      List<String> strings = new ArrayList<>();
      strings.add("a");
      strings.add("b");
      p("string-list", g.toJson(strings));
    });

    step("nulls", () -> {
      p("skip-null", new Gson().toJson(new Bag()));
      p("serialize-null", new GsonBuilder().serializeNulls().create().toJson(new Bag()));
    });

    step("order", () -> {
      Map<String, Integer> m = new LinkedHashMap<>();
      m.put("z", 1);
      m.put("a", 2);
      m.put("m", 3);
      p("map-order", new Gson().toJson(m));
      p("field-order", new Gson().toJson(new Ordered()));
    });

    step("escaping", () -> {
      p("html-escape", new Gson().toJson(new Str("<b>&='")));
      p("no-html-escape",
          new GsonBuilder().disableHtmlEscaping().create().toJson(new Str("<b>&='")));
    });

    step("unicode", () -> {
      Gson g = new Gson();
      String json = g.toJson(new Str("日本"));
      p("unicode-escaped", String.valueOf(json.indexOf('\\') >= 0));
      StringBuilder cp = new StringBuilder();
      String back = g.fromJson(json, Str.class).s;
      for (int i = 0; i < back.length(); i++) {
        if (i > 0) {
          cp.append(',');
        }
        cp.append(Integer.toHexString(back.charAt(i)));
      }
      p("unicode-roundtrip", cp.toString());
    });

    step("parse", () -> {
      Gson g = new Gson();
      p("parse-int", String.valueOf(g.fromJson("{\"a\":7}", Ordered.class).a == 7 ? 7 : -1));
      p("parse-unquoted-name", String.valueOf(g.fromJson("{a:7}", Ordered.class).a));
      p("empty-is-null", String.valueOf(g.fromJson("", Ordered.class)));
      try {
        g.fromJson("{", Ordered.class);
        p("truncated-throws", "no-exception");
      } catch (Throwable t) {
        p("truncated-throws", t.getClass().getName());
      }
      JsonReader strict = new JsonReader(new StringReader("{a:7}"));
      strict.setLenient(false);
      try {
        strict.beginObject();
        strict.nextName();
        p("strict-throws", "no-exception");
      } catch (Throwable t) {
        p("strict-throws", t.getClass().getName());
      }
    });

    step("generics", () -> {
      Gson g = new Gson();
      List<Integer> ints = g.fromJson("[1,2,3]", new TypeToken<List<Integer>>() {}.getType());
      p("int-list", ints.size() + ":" + ints.get(0).getClass().getName());
      Map<String, List<Integer>> deep =
          g.fromJson("{\"a\":[1,2]}", new TypeToken<Map<String, List<Integer>>>() {}.getType());
      p("nested-generic", "a=" + deep.get("a").size());
    });

    step("names", () -> {
      p("serialized-name", new Gson().toJson(new Named()));
      p("alternate-name", new Gson().fromJson("{\"alt\":\"v\"}", Alt.class).k);
      p("naming-policy", new GsonBuilder()
          .setFieldNamingPolicy(FieldNamingPolicy.LOWER_CASE_WITH_UNDERSCORES)
          .create().toJson(new Camel()));
    });

    step("pretty", () -> {
      JsonObject o = new JsonObject();
      o.addProperty("a", 1);
      p("pretty", new GsonBuilder().setPrettyPrinting().create().toJson(o));
    });

    step("exclusion", () -> {
      p("expose-only", new GsonBuilder().excludeFieldsWithoutExposeAnnotation()
          .create().toJson(new Exposed()));
      p("since-version", new GsonBuilder().setVersion(1.0).create().toJson(new Versioned()));
    });

    step("tree", () -> {
      JsonObject o = new JsonObject();
      o.addProperty("n", 1);
      p("jsonobject-tostring", o.toString());
      p("parse-tree",
          String.valueOf(JsonParser.parseString("{\"a\":5}").getAsJsonObject().get("a").getAsInt()));
      JsonArray arr = new JsonArray();
      arr.add(1);
      JsonArray copy = arr.deepCopy();
      arr.add(2);
      p("deep-copy", copy.size() + ":" + arr.size());
    });

    step("stream", () -> {
      StringWriter out = new StringWriter();
      JsonWriter w = new JsonWriter(out);
      w.beginObject();
      w.name("k");
      w.value(1);
      w.endObject();
      w.close();
      p("writer-out", out.toString());
      JsonReader r = new JsonReader(new StringReader("{\"a\":[1]}"));
      StringBuilder tokens = new StringBuilder();
      while (true) {
        String token = r.peek().name();
        if (tokens.length() > 0) {
          tokens.append(',');
        }
        tokens.append(token);
        if ("END_DOCUMENT".equals(token)) {
          break;
        }
        switch (token) {
          case "BEGIN_OBJECT": r.beginObject(); break;
          case "END_OBJECT": r.endObject(); break;
          case "BEGIN_ARRAY": r.beginArray(); break;
          case "END_ARRAY": r.endArray(); break;
          case "NAME": r.nextName(); break;
          case "NUMBER": r.nextInt(); break;
          default: r.skipValue(); break;
        }
      }
      r.close();
      p("reader-tokens", tokens.toString());
    });

    step("adapter", () -> {
      Gson g = new Gson();
      TypeAdapter<String> a = g.getAdapter(String.class);
      p("adapter-tojson", a.toJson("x"));
      p("adapter-null", a.nullSafe().toJson(null));
    });

    step("numbers", () -> {
      Gson g = new Gson();
      p("number-fields", g.toJson(new Numbers()));
      p("object-number-class", g.fromJson("1", Object.class).getClass().getName());
      p("bigdecimal", g.fromJson("1.10", BigDecimal.class).toString());
    });

    step("enums", () -> {
      p("enum-name", new Gson().toJson(new Holder()));
      p("enum-parse", String.valueOf(new Gson().fromJson("{\"e\":\"A\"}", Holder.class).e));
    });

    step("records", () -> {
      Gson g = new Gson();
      p("record-write", g.toJson(new Pair(1, "x")));
      Pair back = g.fromJson("{\"a\":2,\"b\":\"y\"}", Pair.class);
      p("record-read", back.a() + "|" + back.b());
    });

    step("build-config", () -> {
      p("build-version", com.google.gson.internal.GsonBuildConfig.VERSION);
    });

    System.out.flush();
    System.out.println("''' + OK_LINE + r'''");
    System.out.flush();
  }
}
'''

MODULE_SOURCE = r'''
import com.google.gson.Gson;
import java.lang.module.ModuleDescriptor;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Set;

/**
 * Resolves the delivered gson jar as a named module and reports what the module
 * system read out of it.
 *
 * The class-level checks elsewhere in this suite read
 * META-INF/versions/9/module-info.class as bytes.  This asks a different
 * question: whether the JVM, handed the whole jar on the module path, arrives at
 * that descriptor.  A jar missing `Multi-Release: true` answers with an
 * automatic module named after the file; a jar with the descriptor at the root
 * answers correctly here and breaks on Java 8; only the multi-release layout
 * answers correctly in both places.
 */
public final class ModuleProbe {

  private static void p(String key, String value) {
    System.out.println(key + "\t" + value);
  }

  private static String join(List<String> items) {
    Collections.sort(items);
    return String.join(",", items);
  }

  public static void main(String[] args) {
    Module m = Gson.class.getModule();
    p("mode", m.isNamed() ? "module" : "classpath");
    p("module-name", String.valueOf(m.getName()));
    ModuleDescriptor d = m.getDescriptor();
    if (d == null) {
      p("module-descriptor", "absent");
    } else {
      p("module-automatic", String.valueOf(d.isAutomatic()));
      p("module-open", String.valueOf(d.isOpen()));
      List<String> exports = new ArrayList<>();
      for (ModuleDescriptor.Exports e : d.exports()) {
        exports.add(e.source() + (e.isQualified() ? "(qualified)" : ""));
      }
      p("module-exports", join(exports));
      List<String> requires = new ArrayList<>();
      List<String> statics = new ArrayList<>();
      for (ModuleDescriptor.Requires r : d.requires()) {
        requires.add(r.name());
        if (r.modifiers().contains(ModuleDescriptor.Requires.Modifier.STATIC)) {
          statics.add(r.name());
        }
      }
      p("module-requires", join(requires));
      p("module-requires-static", join(statics));
      Set<String> packages = d.packages();
      List<String> internal = new ArrayList<>();
      for (String pkg : packages) {
        if (pkg.contains(".internal")) {
          internal.add(pkg);
        }
      }
      p("module-packages", String.valueOf(packages.size()));
      p("module-internal-packages", join(internal));
    }
    p("exported-api", new Gson().toJson(new int[] {1, 2, 3}));
    try {
      Class<?> cfg = Class.forName("com.google.gson.internal.GsonBuildConfig");
      Object value = cfg.getField("VERSION").get(null);
      p("internal-access", "readable:" + value);
    } catch (Throwable t) {
      p("internal-access", t.getClass().getName());
    }
    System.out.flush();
    System.out.println("''' + OK_LINE + r'''");
    System.out.flush();
  }
}
'''

EXTRAS_SOURCE = r'''
import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.typeadapters.RuntimeTypeAdapterFactory;
import com.google.gson.typeadapters.UtcDateTypeAdapter;
import java.util.Date;
import java.util.TimeZone;

/** Uses the delivered gson-extras jar against the delivered gson jar. */
public final class ExtrasProbe {

  private static void p(String key, String value) {
    System.out.println(key + "\t" + value);
  }

  abstract static class Shape {
    int id = 3;
  }

  static class Circle extends Shape {
    double r = 1.5d;
  }

  public static void main(String[] args) {
    TimeZone.setDefault(TimeZone.getTimeZone("UTC"));
    p("extras-loaded", RuntimeTypeAdapterFactory.class.getName());
    try {
      RuntimeTypeAdapterFactory<Shape> factory =
          RuntimeTypeAdapterFactory.of(Shape.class, "kind")
              .registerSubtype(Circle.class, "circle");
      Gson g = new GsonBuilder().registerTypeAdapterFactory(factory).create();
      Shape s = new Circle();
      p("runtime-type-write", g.toJson(s, Shape.class));
      Shape back = g.fromJson("{\"kind\":\"circle\",\"id\":4,\"r\":2.0}", Shape.class);
      p("runtime-type-read", back.getClass().getSimpleName() + ":" + back.id);
    } catch (Throwable t) {
      p("runtime-type.error", t.getClass().getName() + ": " + t.getMessage());
    }
    try {
      Gson g = new GsonBuilder()
          .registerTypeAdapter(Date.class, new UtcDateTypeAdapter()).create();
      p("utc-date-write", g.toJson(new Date(0L)));
      p("utc-date-read",
          String.valueOf(g.fromJson("\"1970-01-01T00:00:01.000Z\"", Date.class).getTime()));
    } catch (Throwable t) {
      p("utc-date.error", t.getClass().getName() + ": " + t.getMessage());
    }
    System.out.flush();
    System.out.println("''' + OK_LINE + r'''");
    System.out.flush();
  }
}
'''

METRICS_SOURCE = r'''
import com.google.gson.Gson;
import com.google.gson.metrics.BagOfPrimitives;

/** Uses the delivered gson-metrics jar against the delivered gson jar. */
public final class MetricsProbe {

  private static void p(String key, String value) {
    System.out.println(key + "\t" + value);
  }

  public static void main(String[] args) {
    p("metrics-loaded", BagOfPrimitives.class.getName());
    p("metrics-json", new Gson().toJson(new BagOfPrimitives(10L, 20, false, "stringValue")));
    try {
      // Loaded without initialising: caliper is a compile dependency of this
      // module, and the claim under test is that the class reached the jar.
      Class<?> runner = Class.forName("com.google.gson.metrics.NonUploadingCaliperRunner",
          false, MetricsProbe.class.getClassLoader());
      p("metrics-runner", runner.getName());
    } catch (Throwable t) {
      p("metrics-runner.error", t.getClass().getName() + ": " + t.getMessage());
    }
    System.out.flush();
    System.out.println("''' + OK_LINE + r'''");
    System.out.flush();
  }
}
'''

PROTO_SOURCE = r'''
import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonObject;
import com.google.gson.protobuf.ProtoTypeAdapter;
import com.google.protobuf.DescriptorProtos.FieldDescriptorProto;
import com.google.protobuf.Message;

/**
 * Uses the delivered gson-proto jar against the delivered gson jar.
 *
 * The message type is one protobuf-java ships itself, so this exercises real
 * serialisation without depending on protoc having generated anything -- whether
 * the build regenerated gson's own test messages is the `derive` module's
 * question, asked there against the build tree.
 */
public final class ProtoProbe {

  private static void p(String key, String value) {
    System.out.println(key + "\t" + value);
  }

  public static void main(String[] args) {
    p("proto-loaded", ProtoTypeAdapter.class.getName());
    try {
      ProtoTypeAdapter adapter = ProtoTypeAdapter.newBuilder()
          .setEnumSerialization(ProtoTypeAdapter.EnumSerialization.NUMBER)
          .build();
      p("proto-adapter", adapter.getClass().getSimpleName());
      Gson gson = new GsonBuilder()
          .registerTypeHierarchyAdapter(Message.class, adapter)
          .create();
      FieldDescriptorProto msg = FieldDescriptorProto.newBuilder()
          .setName("field_one")
          .setNumber(7)
          .setType(FieldDescriptorProto.Type.TYPE_STRING)
          .build();
      JsonObject json = gson.toJsonTree(msg).getAsJsonObject();
      p("proto-name", json.get("name").getAsString());
      p("proto-number", String.valueOf(json.get("number").getAsInt()));
      // NUMBER serialization was asked for above, so the enum arrives as its
      // wire value rather than as TYPE_STRING.
      p("proto-enum-number", String.valueOf(json.get("type").getAsInt()));
      FieldDescriptorProto back =
          gson.fromJson(json, FieldDescriptorProto.class);
      p("proto-roundtrip", back.getName() + "|" + back.getNumber() + "|"
          + back.getType().name());
      p("proto-equal", String.valueOf(back.equals(msg)));
    } catch (Throwable t) {
      p("proto-adapter.error", t.getClass().getName() + ": " + t.getMessage());
    }
    System.out.flush();
    System.out.println("''' + OK_LINE + r'''");
    System.out.flush();
  }
}
'''

