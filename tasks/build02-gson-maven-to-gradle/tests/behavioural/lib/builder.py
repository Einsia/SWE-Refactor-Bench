#!/usr/bin/env python3
"""Run the delivered build, once per configuration, from clean sources.

Every `Build` gets its own copy of the delivered tree, so configurations cannot
contaminate each other and a mutation probe cannot damage the tree the rest of
the suite reads.  Copies are cheap next to a build over 257 files.

A configuration names an *intent* -- the four jars, with or without the test
task, with or without a publication -- and `dialect` renders it into the argv of
whichever build system the delivered tree declares.  See `dialect.py` for why
this stage speaks both: State A is the oracle every expectation in `data/` was
recorded from, and a stage that cannot build its own oracle cannot show its
expectations are satisfiable.  Whether the migration happened is stage 1's
question, asked over the files by a reviewer, with required gates.

A build that fails does not raise.  It records `ok=False` and the part of the log
a human would look at first, and the modules that needed it fail on that, with
the log attached.  One broken configuration must not error the session out of
collection -- a submission whose `publish` does not work should still be paid for
the four jars its `assemble` produced.

Environment rules, applied to every invocation:

  * the stub directory is first on PATH, so any attempt to reach a build engine
    the delivered tree does not declare -- `ant`, `make`, `bazel`, and for a
    Gradle submission `mvn` too -- fails with 127 and is recorded with its argv
    and cwd;
  * offline, because the container has no network and the build should say so
    rather than spend the timeout retrying;
  * no daemon, so nothing survives between configurations;
  * `HOME` stays `/root`, because `mavenLocal()` and the offline init script the
    environment supplies both live there.  A build that only resolves because the
    agent's own `GRADLE_OPTS` leaked in is not being measured, so those are
    dropped.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import time
import zipfile

import dialect

from swerefactor.contract import submission_env

REPO = os.environ.get("SRB_REPO", "/workspace/repo")
# The trees live under $SRB_SUITE_WORK rather than a module's private scratch:
# the `build` module runs the whole matrix once and every other module reads
# these same directories back.  Falling through to $SRB_WORK keeps this
# importable in a one-off shell where only the module variable is set.
WORK = (os.environ.get("SRB_SUITE_WORK") or os.environ.get("SRB_WORK")
        or "/tmp/srb-work")
STUBS = os.environ.get("SRB_STUB_DIR", "/tmp/srb-stubs")
DELIVERED = "/tmp/srb-delivered"

VERSION = "2.10.1"
GROUP = "com.google.code.gson"

# project directory -> the jar State A's build produced for it.  `proto` is the
# odd one: its artifactId is `proto` but its pom sets `<finalName>gson-proto</finalName>`,
# so the file has no version in its name.  A port that derives every jar name
# from the artifact id gets three right and that one wrong, which is exactly the
# kind of detail this suite is for.
PROJECTS = ("gson", "extras", "metrics", "proto")
JAR_NAMES = {
    "gson": "gson-%s.jar" % VERSION,
    "extras": "gson-extras-%s.jar" % VERSION,
    "metrics": "gson-metrics-%s.jar" % VERSION,
    "proto": "gson-proto.jar",
}
# The ground-truth files are keyed by artifact, not by directory.
ARTIFACTS = {"gson": "gson", "extras": "gson-extras",
             "metrics": "gson-metrics", "proto": "gson-proto"}
ARTIFACT_DIR = {v: k for k, v in ARTIFACTS.items()}

# Jars a build may legitimately produce beside the four, and which are therefore
# not "the" jar of a project when the expected name is missing.
SIDECAR_SUFFIXES = ("-sources.jar", "-javadoc.jar", "-tests.jar",
                    "-test.jar", "-all.jar", "-shaded.jar")


def source_root():
    """The delivered sources: the snapshot if prepare_workspace made one."""
    return DELIVERED if os.path.isdir(DELIVERED) else REPO


def delivered_dialect():
    """The build system the delivered tree declares.  See dialect.detect()."""
    return dialect.for_tree(source_root())


def clean_env(extra=None):
    env = submission_env()
    env["PATH"] = STUBS + ":" + env.get("PATH", "/usr/bin:/bin")
    env["LC_ALL"] = "C.UTF-8"
    env["LANG"] = "C.UTF-8"
    env["TZ"] = "UTC"
    env["TERM"] = "dumb"
    # HOME is deliberately *not* redirected: /root/.gradle/init.d holds the
    # offline init script the environment supplies to the agent as well, and
    # /root/.m2/repository is the vendored closure that makes `mavenLocal()`
    # resolve.  Both are environment, not submission, and both are documented.
    env.setdefault("GRADLE_USER_HOME", "/root/.gradle")
    # Nothing may leak in from the agent's own shell.  A build that only
    # succeeds because MAVEN_OPTS or GRADLE_OPTS was set is not the build.
    for k in ("GRADLE_OPTS", "MAVEN_OPTS", "MAVEN_ARGS", "JAVA_TOOL_OPTIONS",
              "_JAVA_OPTIONS", "ANT_HOME", "ANT_OPTS", "M2_HOME", "CLASSPATH"):
        env.pop(k, None)
    if extra:
        env.update(extra)
    return env


class Mutation:
    """One edit applied to the copied sources before the build runs.

    This is the instrument for the question a byte-comparison cannot ask.  Every
    entry-set and checksum check in this suite is satisfiable by a build that
    enumerates the 282 known entry names; none of them survives a source that did
    not exist when the ground truth was frozen.  `write` adds one, `replace`
    changes one that is already there, and the *shape* of the path decides which
    jar has to react.
    """

    def __init__(self, kind, path, old=None, new=None, text=None, data=None):
        self.kind = kind          # write | binary | replace | delete
        self.path = path
        self.old = old
        self.new = new
        self.text = text
        self.data = data

    def apply(self, root):
        full = os.path.join(root, self.path)
        if self.kind == "delete":
            if os.path.isfile(full):
                os.remove(full)
                return True
            return False
        if self.kind == "write":
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="") as fh:
                fh.write(self.text)
            return True
        if self.kind == "binary":
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as fh:
                fh.write(self.data)
            return True
        if self.kind == "replace":
            if not os.path.isfile(full):
                return False
            with open(full, encoding="utf-8", newline="") as fh:
                src = fh.read()
            if self.old not in src:
                return False
            with open(full, "w", encoding="utf-8", newline="") as fh:
                fh.write(src.replace(self.old, self.new, 1))
            return True
        raise ValueError(self.kind)


class Build:
    """One build invocation against a fresh copy of the delivered sources.

    `intent` says what the invocation is for -- see `dialect.INTENTS` -- and the
    delivered tree's dialect decides the argv.  `second_intent` re-runs in the
    same tree, for idempotence.
    """

    def __init__(self, name, intent="assemble", props=None,
                 mutations=(), subdir=None, extra_args=(), timeout=3600,
                 extra_env=None, second_intent=None, snapshot_libs=False,
                 publish_dir=None, dialect_name=None):
        self.name = name
        self.intent = intent
        self.props = dict(props or {})
        self.mutations = list(mutations)
        # `subdir` relocates the sources inside the work directory, which is how
        # "no absolute path is baked into the build" gets asked.
        self.subdir = subdir or "repo"
        self.extra_args = list(extra_args)
        self.timeout = timeout
        self.extra_env = dict(extra_env or {})
        # `second_intent` re-runs the build in the same tree, for idempotence.
        self.second_intent = second_intent
        # Which dialect renders the intent.  Detected from the delivered tree once,
        # here, and carried in the ledger: every module has to agree about what was
        # run, and a restored Build is read rather than re-detected.
        self.dialect_name = dialect_name or dialect.detect(source_root()) \
            or dialect.GRADLE
        self.engine = dialect.get(self.dialect_name)
        # What dialect.prepare() did to the copied sources, for the ledger.
        self.inputs_applied = []
        # With `snapshot_libs`, every build/libs directory is copied aside between
        # the two passes, so the first pass's jars survive the second pass's clean
        # and the two can be compared entry by entry.
        self.snapshot_libs = snapshot_libs
        # A relative -P<publish_property> value, resolved by the build itself.
        self.publish_dir = publish_dir

        self.dir = os.path.join(WORK, name)
        self.root = os.path.join(self.dir, self.subdir)

        self.ran = False
        self.ok = False
        self.rc = None
        self.rc2 = None
        self.log = ""
        self.log2 = ""
        self.log_path = None
        self.mutations_applied = []
        self.seconds = 0.0

    # ------------------------------------------------------------------ setup
    def _prepare(self):
        if os.path.isdir(self.dir):
            shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(os.path.dirname(self.root), exist_ok=True)
        shutil.copytree(source_root(), self.root, symlinks=True,
                        ignore=shutil.ignore_patterns(".git", ".gradle",
                                                      "target"))
        # Some inputs cannot be passed on a command line in every dialect -- the
        # version is a property to Gradle and the POM's own `<version>` to Maven --
        # so the dialect gets to write them into the copy first.  Before the
        # mutations, so a mutation that edits a build file still has the last word.
        self.inputs_applied = self.engine.prepare(self.root, self.props)
        for m in self.mutations:
            self.mutations_applied.append((m.kind, m.path, m.apply(self.root)))

    def _cmd(self, intent):
        return self.engine.argv(intent, self.props, self.publish_dir,
                                self.root, self.extra_args)

    # ---------------------------------------------------------------- execute
    def execute(self):
        self.ran = True
        t0 = time.time()
        try:
            self._prepare()
        except Exception as exc:            # noqa: BLE001 - reported, not raised
            self.log = "prepare failed: %r" % (exc,)
            self.seconds = time.time() - t0
            return self
        env = clean_env(self.extra_env)
        self.rc, self.log = self._run(self._cmd(self.intent), env)
        self.log_path = os.path.join(self.dir, "build.log")
        self._write(self.log_path, self.log)
        if self.snapshot_libs and self.rc == 0:
            self._snapshot_libs()
        if self.second_intent is not None and self.rc == 0:
            self.rc2, self.log2 = self._run(self._cmd(self.second_intent), env)
            self._write(os.path.join(self.dir, "build2.log"), self.log2)
        self.ok = (self.rc == 0)
        self.seconds = time.time() - t0
        return self

    def _run(self, cmd, env):
        try:
            p = subprocess.run(cmd, cwd=self.root, env=env, timeout=self.timeout,
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            return p.returncode, p.stdout.decode("utf-8", "replace")
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or b""
            return 124, out.decode("utf-8", "replace") + \
                "\n*** TIMEOUT after %ss ***\n" % self.timeout
        except OSError as exc:
            return 127, "could not run %s: %r" % (cmd[0], exc)

    @staticmethod
    def _write(path, text):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(text)
        except OSError:
            pass

    def _snapshot_libs(self):
        """Copy the first pass's jars aside so the second pass's clean cannot eat
        them.  Every jar `libs()` would find, flattened by file name: which
        directory of the build tree a dialect puts its jars in is its own business,
        and the comparison that reads this one is entry-by-entry between two jars of
        the same name."""
        for project in PROJECTS:
            jars = self.libs(project)
            if not jars:
                continue
            dst = os.path.join(self.dir, "libs-pass1", project)
            try:
                os.makedirs(dst, exist_ok=True)
                for src in jars:
                    target = os.path.join(dst, os.path.basename(src))
                    if not os.path.exists(target):   # libs() is shallowest-first
                        shutil.copy2(src, target)
            except OSError as exc:
                self.log += "\n*** libs snapshot failed for %s: %r ***\n" % (
                    project, exc)

    # ------------------------------------------------------------- artifacts
    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def build_roots(self, project):
        """A project's build output directories, by its dialect's convention.

        `build/` for Gradle, `target/` for Maven.  Which one it is, is the only
        thing about the delivered build system that the artifact readers below
        need to know.
        """
        out = []
        for name in self.engine.build_dirs:
            p = self.path(project, *name.split("/"))
            if os.path.isdir(p):
                out.append(p)
        return out

    def libs(self, project, snapshot=False):
        """Every jar a project produced, wherever in its build directory it is.

        The convention is `<project>/build/libs` for Gradle and `<project>/target`
        for Maven, and a build that puts the jar somewhere else within its own
        build directory has not done anything wrong, so the whole directory is
        searched.  Sorted shallowest-first, so the conventional location wins when
        a build also copies its jar to a distribution folder.
        """
        if snapshot:
            roots = [os.path.join(self.dir, "libs-pass1", project)]
        else:
            roots = self.build_roots(project)
        out = []
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in ("tmp", "docs")]
                for fn in filenames:
                    if fn.endswith(".jar"):
                        out.append(os.path.join(dirpath, fn))
        out.sort(key=lambda p: (p.count(os.sep), p))
        return out

    def jar(self, project, name=None, snapshot=False):
        """The jar named `name` (default: the one State A produced), or None."""
        want = name or JAR_NAMES[project]
        for p in self.libs(project, snapshot=snapshot):
            if os.path.basename(p) == want:
                return p
        return None

    def primary_jar(self, project, snapshot=False):
        """The project's main jar, by name if it is there and by shape if not.

        Every content check reads this rather than `jar()`, and the distinction is
        deliberate: if a submission produced a correct jar under the wrong file
        name, exactly one check should say so.  Charging the name a second time
        through 219 missing entries would make one mistake look like the build
        produced nothing.
        """
        exact = self.jar(project, snapshot=snapshot)
        if exact:
            return exact
        candidates = [p for p in self.libs(project, snapshot=snapshot)
                      if not os.path.basename(p).endswith(SIDECAR_SUFFIXES)]
        # One artifact staged in two places is not an ambiguity.  A build that
        # writes a jar and then rewrites it elsewhere leaves the same basename at
        # two depths -- moditect's add-module-info stages one under `moditect/`
        # before overwriting the real one, and a distribution copy is just as
        # ordinary.  libs() sorts shallowest-first precisely so the conventional
        # location wins here.  Only genuinely different *names* leave the question
        # "which of these is the project's jar" unanswerable, and that is the
        # question this guard exists to refuse to guess at.
        names = {os.path.basename(p) for p in candidates}
        return candidates[0] if len(names) == 1 else None

    def jars(self):
        return {p: self.primary_jar(p) for p in PROJECTS}

    def present_jars(self):
        return {p: v for p, v in self.jars().items() if v}

    @property
    def complete(self):
        """Built, and all four projects produced a jar."""
        return self.ok and len(self.present_jars()) == len(PROJECTS)

    @property
    def named_correctly(self):
        return [p for p in PROJECTS if self.jar(p) is None]

    def entries(self, jar_path):
        with zipfile.ZipFile(jar_path) as zf:
            return sorted(i.filename for i in zf.infolist() if not i.is_dir())

    def read_entry(self, jar_path, entry):
        with zipfile.ZipFile(jar_path) as zf:
            return zf.read(entry)

    # ------------------------------------------------------------ publication
    def publish_root(self):
        """The local Maven repository a `publish` configuration wrote.

        The contract names a directory through -P<publish_property> and says it is
        resolved against the project.  Both the root and a project's own build
        directory are reasonable readings of that, and an absolute path a build
        honoured literally is a third, so all of them are accepted.  What is being
        measured is the repository layout inside, not where a build chose to put
        it.
        """
        name = self.publish_dir
        if not name:
            return None
        if os.path.isabs(name):
            return name if os.path.isdir(name) else None
        candidates = [self.path(name)]
        for d in self.engine.build_dirs:
            candidates.append(self.path(*(d.split("/") + [name])))
        for project in PROJECTS:
            candidates.append(self.path(project, name))
            for d in self.engine.build_dirs:
                candidates.append(self.path(project, *(d.split("/") + [name])))
        for c in candidates:
            if os.path.isdir(c):
                return c
        hits = []
        for dirpath, dirnames, _fn in os.walk(self.root):
            if name in dirnames:
                hits.append(os.path.join(dirpath, name))
        hits.sort(key=lambda p: (p.count(os.sep), p))
        return hits[0] if hits else None

    def published(self, artifact, filename, group=GROUP, version=VERSION):
        """One file inside the published repository layout, or None."""
        root = self.publish_root()
        if root is None:
            return None
        rel = os.path.join(*(group.split(".") + [artifact, version, filename]))
        p = os.path.join(root, rel)
        return p if os.path.isfile(p) else None

    def published_dir(self, artifact, group=GROUP, version=VERSION):
        root = self.publish_root()
        if root is None:
            return None
        p = os.path.join(root, *(group.split(".") + [artifact, version]))
        return p if os.path.isdir(p) else None

    # ---------------------------------------------------------- build outputs
    def outputs(self, project, pattern):
        """Files matching `pattern` anywhere under a project's build directory.

        The only door onto the build tree that a check gets, and it opens onto
        `<project>/build/` alone.  Two of the things this migration has to derive
        -- the protobuf messages the proto tests compile against, and the
        obfuscated copy of gson's test classes -- never enter a jar, so they can
        only be observed as build output.  Restricting the door to `build/` is
        what keeps that from becoming a licence to read the sources next to it:
        which task produced a file, and how, stays invisible here.

        `pattern` is matched against the path relative to the build directory, with
        fnmatch semantics, so `**` is not special and `*` crosses separators.
        """
        out = []
        for root in self.build_roots(project):
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d != "tmp"]
                for fn in filenames:
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, root)
                    if fnmatch.fnmatch(rel, pattern) or \
                            fnmatch.fnmatch(fn, pattern):
                        out.append(full)
        out.sort(key=lambda p: (p.count(os.sep), p))
        return out

    def output(self, project, pattern):
        """The shallowest match for `pattern`, or None."""
        hits = self.outputs(project, pattern)
        return hits[0] if hits else None

    # -------------------------------------------------------------- test runs
    def test_result_dirs(self, project):
        """Every directory under a project's build tree holding JUnit XML.

        Both dialects write the same `TEST-*.xml` format -- Gradle's test task into
        `build/test-results/test/`, surefire into `target/surefire-reports/` -- so
        finding the directories is all that differs, and `jarinspect.parse_junit_dirs`
        reads either.
        """
        out = []
        for root in self.build_roots(project):
            for dirpath, _dirnames, filenames in os.walk(root):
                if any(f.startswith("TEST-") and f.endswith(".xml")
                       for f in filenames):
                    out.append(dirpath)
        return sorted(out)

    # ------------------------------------------------------------- diagnostics
    def failure_summary(self, limit=3000):
        """The part of the log a human would look at first."""
        head = "build %r: rc=%s, %d/%d jars (%s)" % (
            self.name, self.rc, len(self.present_jars()), len(PROJECTS),
            ", ".join(sorted(self.present_jars())) or "none")
        lines = self.log.splitlines()
        keep, seen = [], set()
        marker = re.compile(
            r"(FAILURE:|What went wrong|^\s*> |error:|Caused by:|"
            r"Could not (find|resolve|determine)|BUILD FAILED|BUILD FAILURE|"
            r"Execution failed for task|no such property|Unsupported class file|"
            r"^\[ERROR\]|Failed to execute goal|Non-resolvable)")
        for i, ln in enumerate(lines):
            if marker.search(ln):
                for j in range(max(0, i - 1), min(len(lines), i + 5)):
                    if j not in seen:
                        seen.add(j)
                        keep.append(lines[j])
        if not keep:
            keep = lines[-40:]
        body = "\n".join(keep[:90])
        tail = "\n(full log: %s)" % self.log_path if self.log_path else ""
        return "%s\n%s%s" % (head, body[:limit], tail)

    def tasks_executed(self):
        """Every task path Gradle printed a header for, in order.

        Gradle prints `> Task :gson:compileJava` for each task it actually runs
        and appends ` UP-TO-DATE`, ` SKIPPED`, ` FROM-CACHE` or ` NO-SOURCE` when
        it did not.  The outcome is kept: "the task exists" and "the task did
        work" are different claims, and an idempotence check needs the second.
        """
        out = []
        row = re.compile(r"^> Task (:[\w:.\-]+)\s*(.*)$")
        for line in self.log.splitlines():
            m = row.match(line.strip())
            if m:
                out.append((m.group(1), m.group(2).strip()))
        return out

    def log_has(self, needle):
        return needle in self.log

    def to_json(self):
        return {"name": self.name, "dialect": self.dialect_name,
                "intent": self.intent, "rc": self.rc, "rc2": self.rc2,
                "ok": self.ok, "seconds": round(self.seconds, 1),
                "root": self.root, "jars": sorted(self.present_jars()),
                "mutations": self.mutations_applied,
                "inputs": self.inputs_applied}


def dump_summary(builds, path):
    try:
        with open(path, "w") as fh:
            json.dump([b.to_json() for b in builds], fh, indent=1)
    except OSError:
        pass


# --------------------------------------------------------------------- ledger --
# Each module in this suite is its own process, and the matrix is eight build
# invocations over a tree whose test task alone runs 1328 cases: running it per
# module would cost eight times what it needs to and would let two modules
# disagree about what the build did. The `build` module runs it once, the trees
# stay on disk under $SRB_SUITE_WORK, and this ledger records the part of a Build
# the filesystem does not keep -- the invocation, the exit codes and the logs.
LEDGER_SCHEMA = "swerefactor-build02-builds-v1"

_KWARGS = ("intent", "props", "subdir", "extra_args", "timeout", "extra_env",
           "second_intent", "snapshot_libs", "publish_dir", "dialect_name")


def persist(builds, state_dir):
    """Write every build's logs to disk and return a JSON-safe ledger.

    `mutations` is recorded as what was *applied* rather than as Mutation objects:
    a restored Build is read, never re-executed, so the useful fact is which edits
    landed -- which is also exactly what the `derive` module asserts on before it
    trusts a probe's absence to mean anything.
    """
    os.makedirs(state_dir, exist_ok=True)
    ledger = {"schema": LEDGER_SCHEMA, "builds": {}}
    for build in builds:
        logs = os.path.join(state_dir, "logs-" + build.name)
        os.makedirs(logs, exist_ok=True)
        kwargs = {}
        for key in _KWARGS:
            value = getattr(build, key)
            if isinstance(value, dict):
                value = dict(value)
            elif isinstance(value, (list, tuple)):
                value = list(value)
            kwargs[key] = value
        streams = {}
        for key, text in (("log", build.log), ("log2", build.log2)):
            if text is None:
                continue
            path = os.path.join(logs, key + ".txt")
            with open(path, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(text)
            streams[key] = path
        ledger["builds"][build.name] = {
            "name": build.name,
            "kwargs": kwargs,
            "ran": build.ran,
            "ok": build.ok,
            "rc": build.rc,
            "rc2": build.rc2,
            "seconds": round(build.seconds, 1),
            "dir": build.dir,
            "root": build.root,
            "log_path": build.log_path,
            "mutations_applied": [list(m) for m in build.mutations_applied],
            "inputs_applied": [list(i) for i in build.inputs_applied],
            "streams": streams,
        }
    return ledger


def restore(ledger):
    """Rebuild the Build objects from persist(), reading each log back in full.

    The build trees are untouched on disk, so a restored object answers jar(),
    tasks_executed() and publish_root() from the same directories and the same
    recorded text the build produced.  Paths come from the ledger rather than
    being recomputed, so a module whose $SRB_SUITE_WORK is mounted elsewhere still
    reads the tree that was actually built.
    """
    if ledger.get("schema") != LEDGER_SCHEMA:
        raise ValueError("not a build ledger: schema=%r" % ledger.get("schema"))
    out = {}
    for name, record in (ledger.get("builds") or {}).items():
        kwargs = dict(record["kwargs"])
        build = Build(record["name"], **kwargs)
        build.dir = record["dir"]
        build.root = record["root"]
        build.ran = bool(record.get("ran"))
        build.ok = bool(record.get("ok"))
        build.rc = record.get("rc")
        build.rc2 = record.get("rc2")
        build.seconds = float(record.get("seconds") or 0.0)
        build.log_path = record.get("log_path")
        build.mutations_applied = [tuple(m) for m in
                                   record.get("mutations_applied") or []]
        build.inputs_applied = [tuple(i) for i in
                                record.get("inputs_applied") or []]
        for key, path in (record.get("streams") or {}).items():
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    setattr(build, key, fh.read())
            except OSError as exc:
                setattr(build, key,
                        "*** the recorded output is unreadable: %s ***" % exc)
        out[name] = build
    return out




