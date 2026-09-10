#!/usr/bin/env python3
"""Build a QuickJS tree for a named target, and run programs against the result.

Six builds: two roles crossed with three targets.

    original    State A, unpacked from this suite's checksum-verified copy of
                original.tar.gz -- the tree the task started from.
    submission  the delivered tree, snapshotted before any build touched it.

    x86_64      native, 64-bit, little-endian
    s390x       cross, 64-bit, BIG-endian, run under qemu-s390x
    armhf       cross, 32-bit, little-endian, run under qemu-arm

The role is called `original` and not `reference` deliberately.  There is no
reference solution anywhere in this task, and a name that suggests otherwise invites
the wrong grading rule: comparing a submission target against the *same* target of
State A would require the submission to reproduce State A's big-endian brokenness,
which is the opposite of the task.  What State A supplies is a baseline for the two
targets it is already correct on, and a screen.  The value a submission must produce
on s390x comes from x86-64, not from s390x.

armhf is the screen.  It differs from x86-64 in pointer and `long` width and agrees
with it on byte order, so a key those two disagree on depends on word size, was never
an invariant, and grading a submission against it charges a correct port for the
grader's own arithmetic.  It is not a byte-order screen and cannot be one: a screen
that excluded every key State A answers differently on s390x would exclude the entire
bug this task is about.

Every build runs on a *fresh copy* of its role's snapshot, so nothing one build
leaves behind can reach another, and the submission's own build cannot see the
state of a previous attempt.

The trees live under ``$SRB_SUITE_WORK`` rather than a module's private scratch
because the `build` module produces them and nine other modules read them.  Each
build records its own root in the ledger, so a reader never recomputes the path the
writer chose -- it opens the directory it was told about.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

SHARED = (os.environ.get("SRB_SUITE_WORK") or os.environ.get("SRB_WORK")
          or "/tmp/srb-work")
SUITE = os.environ.get("SRB_SUITE_DIR", "/tests/behavioural")
REPO = os.environ.get("SRB_REPO", "/workspace/repo")

BUILD_ROOT = os.environ.get("SRB_BUILD_ROOT") or os.path.join(SHARED, "builds")
SNAPSHOTS = os.environ.get("SRB_SNAPSHOTS") or os.path.join(SHARED, "snapshots")
ANSWERS = os.path.join(SHARED, "answers")
PROGRAM_DIR = os.path.join(SUITE, "data", "programs")

BUILD_TIMEOUT = 2400
RUN_TIMEOUT = 180
#: qemu costs roughly an order of magnitude; give an emulated run room without
#: turning a hang into a stage timeout.
EMULATED_RUN_TIMEOUT = 600

ROLES = ("original", "submission")


# --------------------------------------------------------------------------- #
# Targets
# --------------------------------------------------------------------------- #

class Target:
    """One (architecture, ABI) this stage builds and runs.

    ``big_endian`` and ``word_bits`` are facts about the ABI, taken from the target
    triple and never from the submission.  `native-order` compares the byte order a
    built interpreter demonstrates against ``big_endian``, which is the one place
    this stage is entitled to an absolute expectation: the s390x ELF ABI is
    big-endian whatever any code in the tree believes.
    """

    def __init__(self, name, cross_prefix, emulator, big_endian, word_bits,
                 sysroot=None, elf_machine=None):
        self.name = name
        self.cross_prefix = cross_prefix
        self.emulator = list(emulator)
        self.big_endian = big_endian
        self.word_bits = word_bits
        self.sysroot = sysroot
        self.elf_machine = elf_machine

    @property
    def emulated(self):
        return bool(self.emulator)

    @property
    def run_timeout(self):
        return EMULATED_RUN_TIMEOUT if self.emulated else RUN_TIMEOUT

    def launch(self, argv):
        """Wrap argv in the emulator, if this target needs one."""
        return self.emulator + list(argv)


TARGETS = {
    "x86_64": Target(
        "x86_64", cross_prefix="", emulator=[],
        big_endian=False, word_bits=64, elf_machine="x86-64"),
    "s390x": Target(
        "s390x", cross_prefix="s390x-linux-gnu-",
        emulator=["qemu-s390x-static", "-L", "/usr/s390x-linux-gnu"],
        big_endian=True, word_bits=64,
        sysroot="/usr/s390x-linux-gnu", elf_machine="IBM S/390"),
    "armhf": Target(
        "armhf", cross_prefix="arm-linux-gnueabihf-",
        emulator=["qemu-arm-static", "-L", "/usr/arm-linux-gnueabihf"],
        big_endian=False, word_bits=32,
        sysroot="/usr/arm-linux-gnueabihf", elf_machine="ARM"),
}

TARGET_NAMES = ("x86_64", "s390x", "armhf")

#: The oracle, the screen and the port target, named once so no module hardcodes
#: any of them.  Read with `original` role for the first two: State A on x86-64
#: supplies the expected answers, State A on armhf decides which are invariant, and
#: s390x is the target where State A is wrong and the submission must not be.
ORACLE_TARGET = "x86_64"
CONTROL_TARGET = "armhf"
PORT_TARGET = "s390x"


# --------------------------------------------------------------------------- #
# Subprocess plumbing
# --------------------------------------------------------------------------- #

def nproc():
    try:
        return max(1, min(len(os.sched_getaffinity(0)), 16))
    except AttributeError:
        return max(1, min(os.cpu_count() or 1, 16))


def clean_env(extra=None):
    """The environment a build or a run gets.

    Two things are stripped and both matter.

    The usual flag variables, so a `CFLAGS` in this container cannot change what a
    submission's build does -- a build whose result depends on the grader's
    environment is not reproducible and its failures are not attributable.

    And every ``SRB_`` variable.  Those name this suite, its work directory and its
    result file, and a `Makefile` that reads one knows it is being graded.  There is
    a stage-1 gate about grader awareness, but a gate is a reader's judgement and
    this is a door that can simply be shut: nothing in a byte-order port needs to
    know the name of the harness building it.
    """
    env = dict(os.environ)
    env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    for k in ("CFLAGS", "LDFLAGS", "CPPFLAGS", "CC", "CXX", "MAKEFLAGS",
              "CROSS_PREFIX", "CONFIG_LTO", "HOST_CC"):
        env.pop(k, None)
    for k in [k for k in env if k.startswith("SRB_")]:
        env.pop(k, None)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env["TZ"] = "UTC"
    if extra:
        env.update(extra)
    return env


class Step:
    """One recorded subprocess invocation."""

    def __init__(self, name, argv, rc, out, seconds, timed_out=False, started=None):
        self.name, self.argv, self.rc = name, argv, rc
        self.out, self.seconds, self.timed_out = out, seconds, timed_out
        self.started = started if started is not None else time.time() - seconds

    @property
    def ok(self):
        return self.rc == 0 and not self.timed_out

    def tail(self, n=40):
        lines = self.out.splitlines()
        return "\n".join(lines[-n:])

    def first_line(self, limit=200):
        for line in self.out.splitlines():
            if line.strip():
                return line.strip()[:limit]
        return ""


def run(name, argv, cwd, timeout, env=None, stdin_text=None):
    """Run argv, capture merged output, never raise on a non-zero exit."""
    started = time.time()
    try:
        proc = subprocess.run(
            list(argv), cwd=cwd, env=env or clean_env(),
            input=stdin_text, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, text=True, errors="replace")
        return Step(name, argv, proc.returncode, proc.stdout or "",
                    time.time() - started, started=started)
    except subprocess.TimeoutExpired as exc:
        out = exc.output or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return Step(name, argv, 124, out + "\n*** timed out after %ss ***" % timeout,
                    time.time() - started, timed_out=True, started=started)
    except OSError as exc:
        return Step(name, argv, 127, "*** could not execute: %s ***" % exc,
                    time.time() - started, started=started)


# --------------------------------------------------------------------------- #
# One build
# --------------------------------------------------------------------------- #

#: What `make` is asked for.
#:
#: The interpreter and the bytecode compiler; the static library the generated-C
#: path links against; the two loadable modules upstream's own tests import
#: (`tests/test_bjson.js` and `examples/test_point.js` do not run without them, and
#: `test_bjson.js` is the one place JavaScript reaches the bytecode serialiser
#: directly); and the two executables that carry a generated blob inside them.
#:
#: `examples/hello` and `examples/test_fib` are the load-bearing additions. Both are
#: built by running the *host's* bytecode compiler to emit C, then compiling that C
#: with the target's compiler -- so each one puts a blob written on this machine's
#: byte order into a binary whose reader runs on the target's. That is the same
#: mechanism as `repl.c`, on a rule a submission is much more likely to miss, and it
#: is measured by running the executable rather than by looking at the Makefile.
#:
#: `examples/hello_module` is deliberately absent: it is built by `qjsc -o`, which
#: shells out to a linker for the architecture `qjsc` was configured with, and on a
#: cross build that is the host's. Asking for it would fail every submission on a
#: limitation of upstream's build that has nothing to do with byte order.
#:
#: CONFIG_LTO is cleared because the cross toolchains here carry no matching linker
#: plugin. It changes nothing about byte order.
MAKE_GOALS = ("qjs", "qjsc", "libquickjs.a", "tests/bjson.so",
              "examples/point.so", "examples/fib.so",
              "examples/hello", "examples/test_fib")

#: Cross builds need one more: upstream's `Makefile` redirects every blob-emitting
#: rule at `./host-qjsc`, a *host* binary built from the same sources with the host
#: compiler, and adds it to PROGS only when CROSS_PREFIX is set. Naming it as a goal
#: makes a cross build that never produced it a build failure here rather than a
#: confusing missing-file error in the first module that reaches for it.
CROSS_GOALS = ("host-qjsc",)


class Build:
    """One role built for one target.

    A restored Build (see `restore`) answers everything except `execute()` from the
    same directory the original build wrote, so a module that reads a build tree
    never has to know whether it is looking at a fresh object or a ledger entry.
    """

    def __init__(self, role, target, root=None, goals=None):
        if role not in ROLES:
            raise ValueError("unknown role %r" % (role,))
        if target not in TARGETS:
            raise ValueError("unknown target %r" % (target,))
        self.role = role
        self.target_name = target
        if goals is None:
            goals = list(MAKE_GOALS)
            if TARGETS[target].cross_prefix:
                goals.extend(CROSS_GOALS)
        self.goals = list(goals)
        self.name = "%s-%s" % (role, target)
        self.root = root or os.path.join(BUILD_ROOT, self.name)
        self.steps = {}
        self.ran = False

    # -- identity ----------------------------------------------------------
    @property
    def target(self):
        return TARGETS[self.target_name]

    @property
    def snapshot(self):
        return os.path.join(SNAPSHOTS, self.role)

    def __repr__(self):
        return "<Build %s rc=%s>" % (
            self.name, self.steps["make"].rc if "make" in self.steps else "?")

    # -- state -------------------------------------------------------------
    def step(self, key):
        return self.steps.get(key)

    @property
    def built(self):
        s = self.steps.get("make")
        return bool(s and s.ok) and all(
            os.path.exists(os.path.join(self.root, g)) for g in self.goals)

    def failure_summary(self):
        """One line naming the earliest thing that went wrong."""
        for key in ("prepare", "make"):
            s = self.steps.get(key)
            if s is not None and not s.ok:
                what = "timed out" if s.timed_out else "exited %d" % s.rc
                return "%s: %s %s" % (self.name, key, what)
        missing = [g for g in self.goals
                   if not os.path.exists(os.path.join(self.root, g))]
        if missing:
            return "%s: make succeeded but did not produce %s" % (
                self.name, ", ".join(missing))
        if not self.ran:
            return "%s: never built" % self.name
        return ""

    # -- the build ---------------------------------------------------------
    def prepare_sources(self):
        """Fresh copy of this role's snapshot, so no build sees another's leavings."""
        started = time.time()
        if os.path.exists(self.root):
            shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(os.path.dirname(self.root), exist_ok=True)
        try:
            shutil.copytree(self.snapshot, self.root, symlinks=True)
            out = "copied %s -> %s" % (self.snapshot, self.root)
            rc = 0
        except OSError as exc:
            out, rc = "*** %s ***" % exc, 1
        self.steps["prepare"] = Step("prepare", ["cp", "-a", self.snapshot, self.root],
                                     rc, out, time.time() - started)
        return self.steps["prepare"].ok

    def make_argv(self, goals=None):
        argv = ["make", "CONFIG_LTO=", "-j%d" % nproc()]
        if self.target.cross_prefix:
            argv.append("CROSS_PREFIX=%s" % self.target.cross_prefix)
        argv.extend(goals if goals is not None else self.goals)
        return argv

    def execute(self):
        """Copy, then make.  Records both; never raises."""
        self.ran = True
        if not self.prepare_sources():
            return self
        self.steps["make"] = run("make", self.make_argv(), self.root,
                                 BUILD_TIMEOUT)
        return self

    def make_extra(self, key, goals, timeout=BUILD_TIMEOUT):
        """An additional `make` in an already-built tree, recorded under `key`."""
        self.steps[key] = run(key, self.make_argv(goals), self.root, timeout)
        return self.steps[key]

    # -- what it produced --------------------------------------------------
    def path(self, *parts):
        return os.path.join(self.root, *parts)

    @property
    def qjs(self):
        """The interpreter built *for* this target.  Runs under the emulator."""
        return self.path("qjs")

    @property
    def host_qjsc(self):
        """The bytecode compiler that runs *here*, on the build machine.

        This is the crux of the task and the reason for two properties instead of
        one.  A cross build cannot run a compiler it just built for another
        architecture, so upstream's `Makefile` builds a second copy of `qjsc` with
        the host compiler and points every blob-emitting rule at it.  That host
        binary decides the byte order of `repl.c`, `qjscalc.c`, `hello.c` and
        `test_fib.c` -- blobs the *target's* interpreter then has to read.

        On a native build the two are the same file, which is exactly why State A
        works on x86-64 and crashes on s390x, and why a submission that only tested
        natively can believe it is finished.

        Falls back to `qjsc` so a submission that reorganised the build into a single
        compiler that handles both orders is measured on what it produces rather than
        failed for not having a file with this name.
        """
        p = self.path("host-qjsc")
        return p if os.path.exists(p) else self.path("qjsc")

    @property
    def target_qjsc(self):
        """The bytecode compiler built for the target.  Runs under the emulator.

        Useful for one comparison nothing else makes: what a same-order compiler
        writes for this target, against what the host's cross-order compiler wrote.
        """
        return self.path("qjsc")

    def artifact_kind(self, rel):
        """`file -b` on one produced artifact, or None if it is absent.

        Used to confirm a cross build really produced a cross binary: a `Makefile`
        edited to drop CROSS_PREFIX builds cleanly and produces three x86-64 trees,
        every one of which passes every behavioural probe on the native machine.
        """
        p = self.path(rel)
        if not os.path.exists(p):
            return None
        step = run("file", ["file", "-b", p], self.root, 60)
        return step.out.strip() if step.ok else None

    # -- running it --------------------------------------------------------
    def run_js(self, js_path, qjs_args=(), argv_tail=(), stdin_text=None,
               key=None, timeout=None, cwd=None):
        """Run one JavaScript file under this build's interpreter.

        `cwd` defaults to the build root because upstream's tests import relative
        paths (`./bjson.so`, `./point.so`) and resolve them against the working
        directory.
        """
        argv = self.target.launch(
            [self.qjs] + list(qjs_args) + [js_path] + list(argv_tail))
        return run(key or ("qjs:" + os.path.basename(js_path)), argv,
                   cwd or self.root, timeout or self.target.run_timeout,
                   stdin_text=stdin_text)

    def run_host_qjsc(self, args, key=None, timeout=None):
        """Run the bytecode compiler that executes on the build machine.

        Never wrapped in the emulator: this binary is native by construction, and
        that is the point of it.
        """
        argv = [self.host_qjsc] + list(args)
        return run(key or "host-qjsc", argv, self.root, timeout or RUN_TIMEOUT)

    def run_target_qjsc(self, args, key=None, timeout=None):
        """Run the bytecode compiler built for the target, under the emulator."""
        argv = self.target.launch([self.target_qjsc] + list(args))
        return run(key or "target-qjsc", argv, self.root,
                   timeout or self.target.run_timeout)

    def cc_argv(self, out, sources, extra=()):
        """A compile-and-link command for this target, for a generated .c file.

        The one place this stage compiles something itself.  `qjsc -o` would do it,
        but `qjsc -o` shells out to the compiler it was *configured* with, which on
        a cross build is the host's -- so it links a target blob against a host
        library and fails on relocations.  Driving the cross compiler directly
        reaches the same executable without asking upstream's build for something it
        was never designed to do.
        """
        cc = self.target.cross_prefix + "gcc"
        argv = [cc, "-O2", "-o", out]
        argv.extend(sources)
        argv.extend(extra)
        argv.extend([self.path("libquickjs.a"), "-lm", "-ldl", "-lpthread"])
        return argv

    def compile_generated(self, out, generated_c, key=None):
        """Compile a `qjsc -e` output into an executable for this target."""
        return run(key or "cc:" + os.path.basename(out),
                   self.cc_argv(out, [generated_c, "-I", self.root]),
                   self.root, RUN_TIMEOUT)

    def run_native_binary(self, path, argv_tail=(), key=None, timeout=None):
        """Run an executable built for this target, under the emulator if needed."""
        argv = self.target.launch([path] + list(argv_tail))
        return run(key or os.path.basename(path), argv, self.root,
                   timeout or self.target.run_timeout)


# --------------------------------------------------------------------------- #
# Programs and answer sheets
# --------------------------------------------------------------------------- #
#
# A program is a JavaScript file under data/programs/ that prints lines of the form
#
#     key: value
#
# to stdout and nothing else.  That is the entire format, and it is the format on
# purpose: stage 3's candidates are asked to write programs in it, so an adversary
# constructs the same kind of artifact this stage grades and the two stages cannot
# drift into measuring different things.
#
# The grading rule is one sentence: on every admitted key, all three submission
# targets must print what State A's x86-64 build printed.
#
# That single sentence covers both halves of the task, which is why it is stated as
# one rule and not two.  On x86-64 and armhf it is a preservation requirement -- State
# A is already correct there, so agreeing with it means nothing was broken in passing.
# On s390x it is the port requirement -- State A is wrong there, so agreeing with
# x86-64 is precisely what the port has to achieve.  A submission that changed nothing
# fails only the s390x column; a submission that byte-swapped indiscriminately fails
# the other two.
#
# `screen()` decides which keys are admitted, and it compares State A's x86-64 and
# armhf builds: two little-endian targets of different word size.  A key they disagree
# on depends on word size, was never an invariant, and is excluded -- reported and
# unscored, which is neutral in numerator and denominator both, so a program whose
# every key turns out non-invariant cannot earn credit for having produced nothing.
#
# The screen deliberately does *not* look at s390x.  A screen that excluded every key
# State A answers differently on s390x would exclude the entire bug this task is
# about; there would be nothing left to grade.  Which means the screen cannot protect
# against a key that is legitimately libc- or ABI-determined on a big-endian target
# and would differ there in a correct port too.  Nothing automatic can: distinguishing
# "s390x differs because the port is missing" from "s390x differs because glibc says
# so" is a question about the C library, not about the submission.
#
# So that protection lives in the programs instead, and it is a real constraint on
# them.  A measured example: `String.prototype.localeCompare` returns libc `memcmp`'s
# magnitude, which is implementation-defined -- glibc on s390x answers -2 where glibc
# on x86-64 answers -1, in a correct build of either. armhf agrees with x86-64, so the
# screen admits the key, and every s390x submission then loses a point to its C
# library. The program was changed to ask for `Math.sign` of the comparison, which is
# all ECMAScript specifies. `data/programs/README.md` states the rule that came out of
# that: a program may only ask questions whose answers the language pins down.
#
# Keys beginning `nat-` are the declared exception: their answer *is* the native byte
# order, so they have no cross-target expectation and are never graded here. The
# `native-order` module grades them as an internal-consistency question -- does this
# build agree with itself about its own ABI -- and also checks the label, because a
# `nat-` key that turns out invariant across a big- and a little-endian build is
# mislabelled and its author should hear about it.

ANSWER_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9_.\-]*):[ \t](.*)$")
NATIVE_PREFIX = "nat-"


def parse_answers(text):
    """`key: value` lines, in order, as a dict.

    A duplicate key keeps the first and records the collision under a reserved
    key, so a program that accidentally prints the same label twice is visible
    rather than silently last-wins.
    """
    out, dupes = {}, []
    for line in text.splitlines():
        m = ANSWER_LINE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).rstrip()
        if key in out:
            dupes.append(key)
            continue
        out[key] = value
    if dupes:
        out["__duplicate_keys__"] = ",".join(sorted(set(dupes)))
    return out


def program_files():
    """Every program, sorted, as (stem, path)."""
    if not os.path.isdir(PROGRAM_DIR):
        return []
    return [(n[:-3], os.path.join(PROGRAM_DIR, n))
            for n in sorted(os.listdir(PROGRAM_DIR)) if n.endswith(".js")]


def program_flags(path):
    """A program may ask for interpreter flags with a first-line directive.

    `// qjs-args: --bignum` on line 1.  Read from the *program*, which is this
    suite's own file, never from the submission.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return []
    m = re.match(r"^//\s*qjs-args:\s*(.+?)\s*$", first)
    return m.group(1).split() if m else []


def collect_answers(build):
    """Run every program under one build.  Returns a JSON-safe answer sheet."""
    sheet = {"role": build.role, "target": build.target_name, "programs": {}}
    for stem, path in program_files():
        step = build.run_js(path, qjs_args=program_flags(path),
                            key="program:" + stem)
        sheet["programs"][stem] = {
            "rc": step.rc,
            "timed_out": step.timed_out,
            "seconds": round(step.seconds, 3),
            "answers": parse_answers(step.out),
            "output_tail": step.tail(20),
        }
    return sheet


def answer_path(name):
    return os.path.join(ANSWERS, name + ".json")


def write_answers(sheet):
    os.makedirs(ANSWERS, exist_ok=True)
    p = answer_path("%s-%s" % (sheet["role"], sheet["target"]))
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(sheet, fh, indent=1, sort_keys=True)
    return p


def read_answers(role, target):
    p = answer_path("%s-%s" % (role, target))
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Comparison and classification
# --------------------------------------------------------------------------- #

def screen(oracle_sheet, control_sheet):
    """Which keys this stage is entitled to grade, and what the answer is.

    `oracle_sheet` is State A on x86-64; `control_sheet` is State A on armhf.  A key
    both printed identically is *admitted*, and the admitted value is the expectation
    every submission target must meet.  A key they printed differently is *excluded*
    -- it depends on word size, which this task does not change -- and a key only one
    of them printed is *missing*.  `nat-` keys are *native* and never graded here.

    Excluded, missing and native keys are all reported and unscored.
    """
    result = {"admitted": {}, "native": {}, "excluded": {}, "missing": {}}
    o_progs = (oracle_sheet or {}).get("programs") or {}
    c_progs = (control_sheet or {}).get("programs") or {}
    for stem in sorted(set(o_progs) | set(c_progs)):
        o = (o_progs.get(stem) or {}).get("answers") or {}
        c = (c_progs.get(stem) or {}).get("answers") or {}
        for key in sorted(set(o) | set(c)):
            if key.startswith("__"):
                continue
            if key.startswith(NATIVE_PREFIX):
                result["native"].setdefault(stem, {})[key] = {
                    ORACLE_TARGET: o.get(key), CONTROL_TARGET: c.get(key)}
            elif key not in o or key not in c:
                result["missing"].setdefault(stem, {})[key] = {
                    ORACLE_TARGET: o.get(key), CONTROL_TARGET: c.get(key)}
            elif o[key] == c[key]:
                result["admitted"].setdefault(stem, {})[key] = o[key]
            else:
                result["excluded"].setdefault(stem, {})[key] = {
                    ORACLE_TARGET: o[key], CONTROL_TARGET: c[key]}
    return result


def grade_sheet(admitted, sheet):
    """Grade one submission answer sheet against the admitted expectations.

    Returns per-program results: `agree` (count), `differ` (key -> {expected,
    actual}), `missing` (admitted keys this run did not print), `graded` (how many
    were expected), plus the run's exit code and output tail.

    A submission may print more keys than were admitted; only admitted keys are
    graded, so a suite revision that adds a program cannot fail a submission for not
    anticipating it, and a submission cannot gain by printing extra lines.  What it
    may not do is print fewer: a missing key is a failure, not an abstention, because
    the alternative rewards a program that crashed halfway through.
    """
    out = {}
    progs = (sheet or {}).get("programs") or {}
    for stem in sorted(set(admitted) | set(progs)):
        run = progs.get(stem) or {}
        want = admitted.get(stem) or {}
        got = {k: v for k, v in (run.get("answers") or {}).items()
               if not k.startswith("__")}
        differ, missing = {}, []
        for key in sorted(want):
            if key not in got:
                missing.append(key)
            elif got[key] != want[key]:
                differ[key] = {"expected": want[key], "actual": got[key]}
        out[stem] = {
            "rc": run.get("rc"),
            "timed_out": run.get("timed_out"),
            "graded": len(want),
            "agree": len(want) - len(differ) - len(missing),
            "differ": differ,
            "missing": missing,
            "extra": sorted(set(got) - set(want)),
            "duplicate_keys": (run.get("answers") or {}).get("__duplicate_keys__"),
            "output_tail": run.get("output_tail"),
        }
    return out


def classify(sheets):
    """Diagnostic only: label every key by how it varies across a role's targets.

    Takes {target_name: sheet} for one role and returns, per program, each key's
    label:

      `invariant`       every target printed the same value
      `order-varying`   the big-endian target differs from the little-endian ones
      `width-varying`   the 32-bit target differs from the 64-bit ones
      `varying`         some other split
      `absent`          at least one target did not print it

    Nothing here changes a score.  It exists so a failure report can say *which kind*
    of answer went wrong, and so `native-order` can check the `nat-` prefix against
    what the build actually did: a `nat-` key labelled `invariant` is mislabelled, and
    on the *submission* role a bare key labelled `order-varying` is a value the port
    left order-dependent -- which is the bug, named.
    """
    labels = {}
    names = [t for t in TARGET_NAMES if t in sheets]
    if len(names) < 2:
        return labels
    be = [t for t in names if TARGETS[t].big_endian]
    le = [t for t in names if not TARGETS[t].big_endian]
    narrow = [t for t in names if TARGETS[t].word_bits < 64]
    wide = [t for t in names if TARGETS[t].word_bits >= 64]

    stems = set()
    for t in names:
        stems |= set(((sheets[t] or {}).get("programs") or {}))
    for stem in sorted(stems):
        per_target = {}
        for t in names:
            answers = (((sheets[t] or {}).get("programs") or {}).get(stem)
                       or {}).get("answers") or {}
            per_target[t] = {k: v for k, v in answers.items()
                             if not k.startswith("__")}
        keys = set()
        for t in names:
            keys |= set(per_target[t])
        for key in sorted(keys):
            values = {t: per_target[t].get(key) for t in names}
            if any(values[t] is None for t in names):
                labels.setdefault(stem, {})[key] = "absent"
                continue
            distinct = set(values.values())
            if len(distinct) == 1:
                label = "invariant"
            elif (be and le
                  and len({values[t] for t in be}) == 1
                  and len({values[t] for t in le}) == 1):
                label = "order-varying"
            elif (narrow and wide
                  and len({values[t] for t in narrow}) == 1
                  and len({values[t] for t in wide}) == 1):
                label = "width-varying"
            else:
                label = "varying"
            labels.setdefault(stem, {})[key] = label
    return labels


def _shared_json_path(name):
    return os.path.join(SHARED, name + ".json")


def write_shared_json(name, data):
    """Publish one of the build module's artifacts for the modules downstream.

    The `build` module writes; every other module reads.  A reader that finds the
    file absent fails naming it rather than recomputing it, because a module that can
    silently reconstruct the screen can silently reconstruct a *different* screen.
    """
    os.makedirs(SHARED, exist_ok=True)
    p = _shared_json_path(name)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    return p


def read_shared_json(name):
    p = _shared_json_path(name)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_screen(data):
    return write_shared_json("screen", data)


def read_screen():
    return read_shared_json("screen")


def write_labels(data):
    return write_shared_json("key-labels", data)


def read_labels():
    return read_shared_json("key-labels")


# --------------------------------------------------------------------------- #
# The ledger
# --------------------------------------------------------------------------- #

LEDGER_SCHEMA = "swerefactor-pf03-builds-v1"


def persist(builds, state_dir):
    """Write every build's streams to disk and return a JSON-safe ledger."""
    os.makedirs(state_dir, exist_ok=True)
    ledger = {"schema": LEDGER_SCHEMA, "builds": {}}
    for name, build in sorted(builds.items()):
        logs = os.path.join(state_dir, "logs-" + name)
        os.makedirs(logs, exist_ok=True)
        record = {
            "role": build.role,
            "target": build.target_name,
            "root": build.root,
            "goals": list(build.goals),
            "ran": build.ran,
            "steps": {},
        }
        for key, step in sorted(build.steps.items()):
            out_path = os.path.join(logs, re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".out")
            with open(out_path, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(step.out)
            record["steps"][key] = {
                "argv": list(step.argv), "rc": step.rc, "seconds": step.seconds,
                "timed_out": step.timed_out, "started": step.started,
                "out_path": out_path,
            }
        ledger["builds"][name] = record
    return ledger


def restore(ledger):
    """Rebuild the Build objects from persist(), reading each log back in full.

    The build trees are untouched on disk, so a restored object answers `built`,
    `artifact_kind()` and `run_js()` from the same directories the build wrote.  It
    is the recorded *steps* that cannot be recovered from the filesystem, which is
    what the ledger is for.
    """
    if ledger.get("schema") != LEDGER_SCHEMA:
        raise ValueError("not a pf03 build ledger: schema=%r" % ledger.get("schema"))
    out = {}
    for name, record in (ledger.get("builds") or {}).items():
        build = Build(record["role"], record["target"], root=record["root"],
                      goals=record.get("goals"))
        build.ran = bool(record.get("ran"))
        for key, step in (record.get("steps") or {}).items():
            try:
                with open(step["out_path"], encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError as exc:
                text = "*** the recorded output is unreadable: %s ***" % exc
            build.steps[key] = Step(key, list(step["argv"]), int(step["rc"]), text,
                                    float(step["seconds"]), bool(step["timed_out"]),
                                    started=float(step["started"]))
        out[name] = build
    return out


