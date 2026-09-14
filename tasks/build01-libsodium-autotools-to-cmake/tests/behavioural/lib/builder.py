#!/usr/bin/env python3
"""Configure / build / install / test the delivered build system.

Every build runs on a *fresh copy* of the delivered sources, out of tree.
Nothing the agent left in the repository can influence a build: the source copy
is made from the pristine snapshot taken before any verifier build ran.

The trees live under ``$SRB_SUITE_WORK`` rather than a module's private scratch
because the `build` module produces them and every other module in the suite
reads them.  Relational rather than a count on purpose: a number here is a second
place the suite's size is written down, and it went stale the last time a module
moved to another stage.
Each build also records its own root in the ledger, so a reader never has to
recompute the path the writer chose -- it opens the directory it was told about.

Two drivers, one interface
--------------------------
The stage measures what the delivered build system *does* -- it installs this
tree, it compiles these 119 units with these flags, it runs these 80 tests -- and
those are questions with an answer whichever build system produced them.  So the
`Build` class detects the flavour of the tree it was handed and drives it
accordingly: ``cmake -S/-B`` + ``cmake --build`` + ``cmake --install`` for a tree
with a CMakeLists.txt, ``autogen.sh`` + ``configure`` + ``make`` + ``make
install`` for one with a configure.ac.  Everything downstream -- the install
tree, the compile database, the registered tests, the option table -- is read
through methods that answer in the same shape either way.

That is not a concession to a submission that skipped the migration.  Stage 1
decides whether the migration happened, over both trees, with a reviewer, and its
gates are required: a repository that still builds with Autotools scores zero
before this image is ever built.  What it buys is that State A -- the oracle every
expectation in ``data/`` was recorded from -- can be run through this stage and
score what it should score, which is everything.  A stage whose own oracle fails
it cannot tell a regression from a baseline.
"""
import json
import os
import re
import shutil
import subprocess
import time

from swerefactor.contract import submission_env

REPO = os.environ.get("SRB_REPO", "/workspace/repo")
SHARED = (os.environ.get("SRB_SUITE_WORK") or os.environ.get("SRB_WORK")
          or "/tmp/srb-work")
BUILD_ROOT = os.environ.get("SRB_BUILD_ROOT") or os.path.join(SHARED, "builds")
DELIVERED = os.environ.get("SRB_DELIVERED") or os.path.join(SHARED, "delivered")
STUBS = os.environ.get("SRB_STUB_DIR") or os.path.join(SHARED, "stubs")

CONFIGURE_TIMEOUT = 900
BUILD_TIMEOUT = 2400
INSTALL_TIMEOUT = 600
CTEST_TIMEOUT = 1800
BOOTSTRAP_TIMEOUT = 900

CMAKE = "cmake"
AUTOTOOLS = "autotools"


def flavour_of(tree):
    """Which build system this source tree ships.

    CMake wins a tie: a tree with both a CMakeLists.txt and a leftover
    configure.ac is a CMake project whose old files were not deleted, and stage 1
    is where that costs something.
    """
    if os.path.isfile(os.path.join(tree, "CMakeLists.txt")):
        return CMAKE
    for name in ("configure", "configure.ac", "autogen.sh", "configure.in"):
        if os.path.exists(os.path.join(tree, name)):
            return AUTOTOOLS
    return CMAKE          # nothing recognisable: report it as a failed cmake -S


def bootstrap_argv_for(tree):
    """How to generate `configure` in an Autotools tree that has no `configure`."""
    script = os.path.join(tree, "autogen.sh")
    if os.path.isfile(script):
        return [script, "-s"]
    return ["autoreconf", "-i"]


def bootstrap_snapshot(tree, timeout=BOOTSTRAP_TIMEOUT):
    """Generate `configure` in the delivered snapshot, once, before the matrix.

    A CMake tree needs nothing here and gets nothing.  An Autotools tree needs
    `configure` to exist, and where that step runs decides what two other checks
    measure:

    * Every build in the matrix copies this snapshot, so bootstrapping here means
      each tree is configured from a `configure` that already existed -- not that
      every one of them ran autoreconf.  One bootstrap per tree would cost minutes
      each and would all produce the same script.
    * `test_out_of_source_build_leaves_sources_clean` compares the snapshot with
      the source copy the build ran in.  Bootstrapping *after* the snapshot is
      taken would put `configure`, `Makefile.in` and `aclocal.m4` on the added
      list of every Autotools build and read as "the build wrote into the
      sources", which is not what happened.

    A released Autotools tarball ships a generated `configure` for the same
    reason: it is a build input, not a build output.  This is the step that makes
    the delivered tree look like one.

    Returns the Step, or None if the tree did not need it.
    """
    if flavour_of(tree) == CMAKE:
        return None
    if os.path.isfile(os.path.join(tree, "configure")):
        return None
    return run("bootstrap", bootstrap_argv_for(tree), tree, timeout,
               clean_env(stubs=False))


def delivered_flavour():
    """The flavour of the tree this run is measuring.

    Read from the ledger the `build` module wrote, so every module in the stage
    agrees even if a build tree is later deleted; a module that runs before the
    ledger exists falls back to the snapshot, then to the repository.
    """
    marker = os.path.join(SHARED, "flavour")
    try:
        with open(marker) as fh:
            got = fh.read().strip()
        if got in (CMAKE, AUTOTOOLS):
            return got
    except OSError:
        pass
    for tree in (DELIVERED, REPO):
        if os.path.isdir(tree):
            return flavour_of(tree)
    return CMAKE


def record_flavour(value):
    os.makedirs(SHARED, exist_ok=True)
    with open(os.path.join(SHARED, "flavour"), "w") as fh:
        fh.write(value)


# --- reading a verbose transcript ------------------------------------------- #
# `make V=1` prints the command it is about to run, but relative to whichever
# directory it has entered, so the entering/leaving lines are part of the record.
ENTER_DIR_RE = re.compile(r"^make(?:\[\d+\])?: Entering directory '([^']+)'")
LEAVE_DIR_RE = re.compile(r"^make(?:\[\d+\])?: Leaving directory ")

#: A compiler driver at the head of the command, after whatever the build tool
#: printed in front of it.  The anchor matters: libtool prints `libtool: compile:
#: gcc -c foo.c` for the invocation it actually runs, and the `--mode=compile`
#: line above it is the wrapper being asked to, which compiles nothing itself --
#: an unanchored match counts both and doubles every unit.
#:
#: Two prefixes are part of the head rather than in front of it.  `libtool:
#: compile:` is one.  Ninja's progress counter is the other: `cmake --build -v`
#: over a Ninja tree prints `[1/401] /usr/bin/cc ...`, and a pattern that admits
#: only `^` and a shell separator sees no compiler on any line of a Ninja
#: transcript -- which reads as a build that compiled nothing, for the generator
#: that is this task's default.
CC_RE = re.compile(r"(?:^|[;&|]\s*)(?:\[\d+/\d+\]\s+)?"
                   r"(?:libtool:\s+compile:\s+)?"
                   r"(?:\S*/)?(?:cc|gcc|clang|c\+\+|g\+\+|clang\+\+|srb-cc)\b")
SOURCE_RE = re.compile(r"(?:^|\s)-c\s+(\S+\.(?:c|S|cc|cpp|cxx))(?:\s|$)")
#: automake names the source through a shell test so a VPATH build finds it
#: either way: ``-c `test -f 'aead.c' || echo '../../src/'`aead.c``.  The name
#: after the closing backtick is the one to resolve.
BACKTICK_SOURCE_RE = re.compile(
    r"`[^`]*`(\S+\.(?:c|S|cc|cpp|cxx))(?:\s|$)")


def _named_source(line):
    for rx in (BACKTICK_SOURCE_RE, SOURCE_RE):
        m = rx.search(line)
        if m:
            return m.group(1)
    return None


def compile_line_source(line, wrappers=False):
    """The source a verbose build line compiles, or None if it compiles nothing.

    A line counts when a compiler driver is being invoked with `-c` on a source
    file: that is a translation unit going through the compiler, whether make ran
    the driver directly or libtool did.

    The libtool *wrapper* line -- ``libtool --mode=compile gcc ... -c foo.c`` --
    is excluded by default.  It names the source too, and the invocation it
    describes is printed separately with the same flags, so counting both would
    double every unit.  `wrappers=True` accepts it, for the one reader that has
    nothing else: a dry run prints what make *would* do, and what it would do is
    run libtool.
    """
    if "--mode=link" in line:
        return None
    if "--mode=compile" in line:
        if not wrappers:
            return None
        return _named_source(line)
    if not CC_RE.search(line):
        return None
    return _named_source(line)


def nproc() -> int:
    try:
        return max(1, min(len(os.sched_getaffinity(0)), 16))
    except AttributeError:
        return max(1, min(os.cpu_count() or 1, 16))


BASE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def clean_env(extra=None, stubs=True):
    """The environment every build in this stage runs under.

    ``stubs`` puts the recording exit-127 Autotools shims first on PATH.  It is
    on for a CMake tree, which must not reach for those tools and is measured on
    not doing so, and off for an Autotools tree, where the shims would shadow the
    build system under test rather than observe it.  The shims are still
    installed either way -- the ledger they write is what turns "the build
    failed" into "the build wanted autoreconf".
    """
    env = submission_env()
    env["PATH"] = (STUBS + ":" + BASE_PATH) if stubs else BASE_PATH
    for k in ("CFLAGS", "LDFLAGS", "CPPFLAGS", "MAKEFLAGS"):
        env.pop(k, None)
    env["LC_ALL"] = "C"
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
        return "\n".join(self.out.splitlines()[-n:])


def run(name, argv, cwd, timeout, env=None):
    t0 = time.time()
    try:
        p = subprocess.run(argv, cwd=cwd, env=env or clean_env(), timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return Step(name, argv, p.returncode, p.stdout.decode("utf-8", "replace"),
                    time.time() - t0, started=t0)
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode("utf-8", "replace")
        return Step(name, argv, 124, out + "\n*** TIMEOUT ***", time.time() - t0,
                    True, started=t0)
    except OSError as exc:
        return Step(name, argv, 127, "OSError: %s" % exc, time.time() - t0,
                    started=t0)


class Build:
    """A single configuration of the agent's build system."""

    def __init__(self, name, generator="Ninja", options=None, build_type="Release",
                 compiler=None, configure_only=False, extra_env=None,
                 export_cc=True, root=None):
        self.name = name
        self.generator = generator
        self.options = dict(options or {})
        self.build_type = build_type
        self.compiler = compiler
        self.configure_only = configure_only
        self.extra_env = dict(extra_env or {})
        self.export_cc = export_cc

        self.root = root or os.path.join(BUILD_ROOT, name)
        self.src = os.path.join(self.root, "src")
        self.bld = os.path.join(self.root, "build")
        self.prefix = os.path.join(self.root, "inst")
        self.destdir = os.path.join(self.root, "destdir")
        self.steps = {}
        self.ran = False
        self._flavour = None

    # -------------------------------------------------------------- flavour --
    @property
    def flavour(self):
        """Which build system drives this tree.

        Resolved from the source copy once it exists, and from the delivered
        snapshot before that, so `cmake_argv()` and friends can be asked before
        `execute()`.
        """
        if self._flavour is None:
            for tree in (self.src, DELIVERED, REPO):
                if os.path.isdir(tree):
                    self._flavour = flavour_of(tree)
                    break
            else:
                self._flavour = CMAKE
        return self._flavour

    @property
    def is_cmake(self):
        return self.flavour == CMAKE

    @property
    def env(self):
        return clean_env(self.extra_env, stubs=self.is_cmake)

    # -------------------------------------------------------------- helpers --
    def step(self, key):
        return self.steps.get(key)

    @property
    def configured(self):
        s = self.step("configure")
        return bool(s and s.ok)

    @property
    def built(self):
        s = self.step("build")
        return bool(s and s.ok)

    @property
    def installed(self):
        s = self.step("install")
        return bool(s and s.ok)

    def failure_summary(self):
        for key in ("bootstrap", "configure", "build", "install"):
            s = self.step(key)
            if s and not s.ok:
                return "%s failed (rc=%d):\n%s" % (key, s.rc, s.tail(30))
        return "ok"

    # ------------------------------------------------------------------ run --
    def prepare_sources(self):
        base = DELIVERED if os.path.isdir(DELIVERED) else REPO
        if os.path.exists(self.root):
            shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(self.root, exist_ok=True)
        shutil.copytree(base, self.src, symlinks=True,
                        ignore=shutil.ignore_patterns(".git"))
        os.makedirs(self.bld, exist_ok=True)

    def cmake_argv(self):
        argv = ["cmake", "-S", self.src, "-B", self.bld, "-G", self.generator,
                "-DCMAKE_INSTALL_PREFIX=" + self.prefix]
        if self.build_type is not None:
            argv.append("-DCMAKE_BUILD_TYPE=" + self.build_type)
        if self.export_cc:
            argv.append("-DCMAKE_EXPORT_COMPILE_COMMANDS=ON")
        if self.compiler:
            argv.append("-DCMAKE_C_COMPILER=" + self.compiler)
        for k, v in sorted(self.options.items()):
            argv.append("-D%s=%s" % (k, v))
        return argv

    # --- the same request, spelled for `configure` ---------------------------
    #
    # The matrix asks for library shapes, not for CMake variables: build the
    # minimal library, build only the shared one, build with no optimisation.
    # These are the two spellings of that request.  A CMake variable with no
    # `configure` equivalent is dropped rather than guessed at, and the check
    # that cared about it skips under `flavour.only`.
    OPTION_MAP = {
        "SODIUM_MINIMAL":      ("--enable-minimal", "--disable-minimal"),
        "SODIUM_BUILD_SHARED": ("--enable-shared", "--disable-shared"),
        "SODIUM_BUILD_STATIC": ("--enable-static", "--disable-static"),
    }
    TRUTHY = {"ON", "1", "TRUE", "YES", "Y"}

    def configure_argv(self):
        script = os.path.join(self.src, "configure")
        argv = [script, "--prefix=" + self.prefix]
        for k, v in sorted(self.options.items()):
            if k in self.OPTION_MAP:
                on, off = self.OPTION_MAP[k]
                argv.append(on if str(v).strip().upper() in self.TRUTHY else off)
            elif k == "CMAKE_INSTALL_LIBDIR":
                argv.append("--libdir=" + os.path.join(self.prefix, str(v)))
            elif k == "CMAKE_C_FLAGS":
                argv.append("CFLAGS=" + str(v))
            elif k.startswith("-"):
                # A switch the caller looked up rather than assumed -- see
                # `feature_switch()`.  `{"--disable-ssp": None}` is the flag on
                # its own, which is how an Autotools switch is spelled.
                argv.append(k if v is None else "%s=%s" % (k, v))
        if self.build_type == "Debug":
            argv.append("--enable-debug")
        if self.compiler:
            argv.append("CC=" + self.compiler)
        return argv

    def bootstrap_argv(self):
        """`autogen.sh -s` if the tree has no generated `configure` yet."""
        return bootstrap_argv_for(self.src)

    @property
    def needs_bootstrap(self):
        return not self.is_cmake and not os.path.isfile(
            os.path.join(self.src, "configure"))

    def execute(self):
        if self.ran:
            return self
        self.ran = True
        self.prepare_sources()
        return self._execute_cmake() if self.is_cmake else self._execute_autotools()

    def _execute_cmake(self):
        env = self.env
        self.steps["configure"] = run("configure", self.cmake_argv(), self.src,
                                      CONFIGURE_TIMEOUT, env)
        if not self.configured or self.configure_only:
            return self

        self.steps["build"] = run(
            "build", ["cmake", "--build", self.bld, "--parallel", str(nproc()), "-v"],
            self.src, BUILD_TIMEOUT, env)
        if not self.built:
            return self

        self.steps["install"] = run(
            "install", ["cmake", "--install", self.bld], self.src,
            INSTALL_TIMEOUT, env)
        return self

    def _execute_autotools(self):
        """configure out of tree, `make V=1`, `make install`.

        Out of tree for the same reason the CMake path is: the source copy is
        compared against the pre-build snapshot afterwards, and an in-tree
        Autotools build writes `Makefile`, `config.h` and every `.o` into it.
        libsodium's build is VPATH-clean, so `../src/configure` from the build
        directory is the whole of it.

        `V=1` because the transcript is evidence: `evidence` counts compiler
        invocations in it and `compile_commands()` synthesises the database from
        it, and automake's silent rules print `CC foo.lo` with no flags at all.
        """
        env = self.env
        if self.needs_bootstrap:
            self.steps["bootstrap"] = run("bootstrap", self.bootstrap_argv(),
                                          self.src, BOOTSTRAP_TIMEOUT, env)
            if not self.steps["bootstrap"].ok:
                # A recorded step whose failure reads as a configure failure, so
                # every downstream check reports "this tree did not configure".
                self.steps["configure"] = self.steps["bootstrap"]
                return self

        self.steps["configure"] = run("configure", self.configure_argv(),
                                      self.bld, CONFIGURE_TIMEOUT, env)
        if not self.configured or self.configure_only:
            return self

        self.steps["build"] = run(
            "build", ["make", "-j", str(nproc()), "V=1"], self.bld,
            BUILD_TIMEOUT, env)
        if not self.built:
            return self

        self.steps["install"] = run("install", ["make", "install"], self.bld,
                                    INSTALL_TIMEOUT, env)
        return self

    # ------------------------------------------------------- the option table --
    #
    # §1.2 asks for four options that are real, boolean, documented and correctly
    # defaulted.  `cmake -LAH` answers that from CMakeCache.txt; `configure
    # --help` and config.status answer it for Autotools.  Both are reduced to the
    # same record so the check reads one shape:
    #
    #     {capability: {"spelling", "kind", "value", "default", "help"}}
    #
    # A capability the delivered build system has no option for is simply absent,
    # and the check that wanted it skips under `flavour.only` rather than failing
    # -- automake builds the test programs under `make check` and nowhere else, so
    # "tests" is a capability with no switch, not a missing option.
    CACHE_RE = re.compile(r"^([A-Za-z_0-9\-]+):([A-Z]+)=(.*)$")

    def cmake_cache(self):
        """{NAME: (type, value)} from CMakeCache.txt."""
        p = os.path.join(self.bld, "CMakeCache.txt")
        entries = {}
        if not os.path.isfile(p):
            return entries
        try:
            text = open(p, errors="replace").read()
        except OSError:
            return entries
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "//")):
                continue
            m = self.CACHE_RE.match(line)
            if m:
                entries[m.group(1)] = (m.group(2), m.group(3))
        return entries

    def cmake_cache_help(self, name):
        """The `//` comment above a cache entry -- what `cmake -LAH` shows."""
        p = os.path.join(self.bld, "CMakeCache.txt")
        if not os.path.isfile(p):
            return ""
        try:
            text = open(p, errors="replace").read()
        except OSError:
            return ""
        m = re.search(r"^//(.*)\n%s:[A-Z]+=" % re.escape(name), text, re.M)
        return m.group(1).strip() if m else ""

    def configure_help(self):
        """`configure --help`, cached -- the Autotools option surface."""
        s = self.steps.get("configure_help")
        if s is None:
            script = os.path.join(self.src, "configure")
            if not os.path.isfile(script):
                return ""
            s = run("configure_help", [script, "--help"], self.bld, 120, self.env)
            self.steps["configure_help"] = s
        return s.out

    def config_status(self):
        """config.status as text -- configure's recorded conclusions."""
        p = os.path.join(self.bld, "config.status")
        try:
            return open(p, errors="replace").read()
        except OSError:
            return ""

    #: `--enable-x`/`--disable-x` and the indented continuation lines under it,
    #: which is where the help text and the documented default live.
    HELP_OPT_RE = r"^  --(enable|disable)-%s\b(.*)((?:\n {6,}.*)*)"

    def _autotools_option(self, feature):
        """One `--enable-FEATURE` as an option record, or None if absent."""
        m = re.search(self.HELP_OPT_RE % re.escape(feature),
                      self.configure_help(), re.M)
        if m is None:
            return None
        spelled, rest, cont = m.group(1), m.group(2), m.group(3)
        text = " ".join((rest + " " + cont).split())
        # The documented default when it is stated, and otherwise the spelling:
        # `--disable-shared` is listed because shared is on, `--enable-minimal`
        # because minimal is off.  That is the same information CMake puts in the
        # cache, written the way Autotools writes it.
        d = re.search(r"\[?default[:=] *(yes|no|enabled|disabled)\]?", text, re.I)
        if d:
            default = "ON" if d.group(1).lower() in ("yes", "enabled") else "OFF"
        else:
            default = "OFF" if spelled == "enable" else "ON"
        # configure's conclusion, when it recorded one.  `enable_shared='no'` is
        # written by libtool; a plain AC_ARG_ENABLE leaves an automake
        # conditional instead, so both are read.
        status = self.config_status()
        value = None
        v = re.search(r"^enable_%s='?([a-z]+)'?" % re.escape(feature), status, re.M)
        if v:
            value = "ON" if v.group(1) in ("yes", "y") else "OFF"
        else:
            c = re.search(r'^S\["%s_TRUE"\]="(.*)"' % re.escape(feature.upper()),
                          status, re.M)
            if c:
                value = "ON" if c.group(1) == "" else "OFF"
        if value is None:
            value = default
        return {"spelling": "--%s-%s" % (spelled, feature), "kind": "BOOL",
                "value": value, "default": default,
                "help": " ".join(text.split()[:40])}

    def options_view(self):
        """{capability: option record} for the four options §1.2 names."""
        import flavour                                   # local: avoids a cycle
        out = {}
        for cap, spelling in sorted(flavour.SWITCHES[self.flavour].items()):
            if spelling is None:
                continue
            if self.is_cmake:
                cache = self.cmake_cache()
                if spelling not in cache:
                    continue
                kind, value = cache[spelling]
                out[cap] = {"spelling": spelling, "kind": kind,
                            "value": value, "default": value,
                            "help": self.cmake_cache_help(spelling)}
            else:
                rec = self._autotools_option(spelling.split("-")[-1])
                if rec is not None:
                    out[cap] = rec
        return out

    def install_to_destdir(self):
        """Re-install with DESTDIR set, in whichever driver's spelling."""
        env = dict(self.env)
        env["DESTDIR"] = self.destdir
        if self.is_cmake:
            return run("destdir", ["cmake", "--install", self.bld], self.src,
                       INSTALL_TIMEOUT, env)
        return run("destdir", ["make", "install", "DESTDIR=" + self.destdir],
                   self.bld, INSTALL_TIMEOUT, env)

    def selected_compiler(self):
        """The C compiler the configured tree will use, as it recorded it."""
        if self.is_cmake:
            return self.cmake_cache().get("CMAKE_C_COMPILER", ("", ""))[1]
        m = re.search(r'^S\["CC"\]="(.*)"', self.config_status(), re.M)
        return m.group(1) if m else ""

    # --------------------------------------------------------- observations --
    def install_entries(self):
        """[(kind, relpath)] of the install tree; kind in f/d/l."""
        out = []
        if not os.path.isdir(self.prefix):
            return out
        for dirpath, dirnames, files in os.walk(self.prefix):
            for d in sorted(dirnames):
                p = os.path.join(dirpath, d)
                rel = os.path.relpath(p, self.prefix)
                out.append(("l" if os.path.islink(p) else "d", rel))
            for f in sorted(files):
                p = os.path.join(dirpath, f)
                rel = os.path.relpath(p, self.prefix)
                out.append(("l" if os.path.islink(p) else "f", rel))
        return sorted(out, key=lambda e: e[1])

    def libdir(self):
        for cand in ("lib", "lib64"):
            p = os.path.join(self.prefix, cand)
            if os.path.isdir(p):
                return p
        return os.path.join(self.prefix, "lib")

    def shared_lib(self):
        """Path to the real (non-symlink) versioned shared object, or None."""
        d = self.libdir()
        if not os.path.isdir(d):
            return None
        best = None
        for f in sorted(os.listdir(d)):
            if not f.startswith("libsodium.so"):
                continue
            p = os.path.join(d, f)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            if re.match(r"^libsodium\.so(\.\d+)*$", f):
                if best is None or len(f) > len(best):
                    best = f
        return os.path.join(d, best) if best else None

    def static_lib(self):
        p = os.path.join(self.libdir(), "libsodium.a")
        return p if os.path.isfile(p) else None

    def pc_file(self):
        p = os.path.join(self.libdir(), "pkgconfig", "libsodium.pc")
        return p if os.path.isfile(p) else None

    def cmake_package_dir(self):
        for base in (self.libdir(), os.path.join(self.prefix, "share")):
            for name in ("cmake/libsodium", "cmake/Libsodium", "cmake/sodium"):
                p = os.path.join(base, name)
                if os.path.isdir(p):
                    return p
        # fall back to any directory holding a plausible config file
        for dirpath, _d, files in os.walk(self.prefix):
            for f in files:
                if re.match(r"^(libsodium|Libsodium|sodium)-?[Cc]onfig\.cmake$", f):
                    return dirpath
        return None

    # -------------------------------------------------------- compile lines --
    def compile_commands(self):
        """[(source_abspath, command_string)] for every compiled unit.

        Read from `compile_commands.json` when the build system emits one, and
        recovered from the verbose transcript when it does not.  The two are the
        same evidence: CMake's database is a record of the commands it ran, and
        `make V=1` prints them.  What the callers ask of either -- which macros
        reached this source, which `-m` flags, whether anything outside the
        repository was compiled -- is answered from the command line itself, so a
        recovered entry is not a weaker one.
        """
        entries = self._json_compile_commands()
        return entries if entries else self._recovered_compile_commands()

    def _json_compile_commands(self):
        p = os.path.join(self.bld, "compile_commands.json")
        if not os.path.isfile(p):
            return []
        try:
            with open(p) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return []
        out = []
        for e in data:
            cmd = e.get("command")
            if cmd is None:
                cmd = " ".join(e.get("arguments", []))
            f = e.get("file", "")
            if not os.path.isabs(f):
                f = os.path.normpath(os.path.join(e.get("directory", ""), f))
            out.append((f, cmd))
        return out

    def _recovered_compile_commands(self):
        """The database `make V=1` prints instead of writing.

        A compile line names its source relatively (`../../src/foo.c`) and is
        printed under whichever directory make had entered, so the entry is only
        usable once the two are put back together: `make[1]: Entering directory`
        lines are tracked and the source is resolved against the current one.

        libtool compiles each unit twice, PIC and not.  Both invocations carry the
        same macros and the same ISA flags -- that is what the callers read -- so
        the first is kept and the duplicate dropped, which also makes the entry
        count the translation-unit count the way CMake's database does.
        """
        s = self.step("build")
        if s is None:
            return self._dryrun_compile_commands()
        entries = self._scan_transcript(s.out)
        # A silent-rules build prints `CC foo.lo` and nothing else.  The wrapper
        # lines are the only remaining channel, so read those rather than report
        # a build that compiled nothing.
        return entries or self._scan_transcript(s.out, wrappers=True)

    def _scan_transcript(self, text, wrappers=False):
        cwd = self.bld
        seen, out = set(), []
        for line in text.splitlines():
            m = ENTER_DIR_RE.match(line)
            if m:
                cwd = m.group(1)
                continue
            if LEAVE_DIR_RE.match(line):
                continue
            src = compile_line_source(line, wrappers=wrappers)
            if src is None:
                continue
            path = src if os.path.isabs(src) else os.path.normpath(
                os.path.join(cwd, src))
            if path in seen:
                continue
            seen.add(path)
            out.append((path, line.strip()))
        return out

    def _dryrun_compile_commands(self):
        """What the build *would* compile, for a tree that was only configured.

        CMake writes compile_commands.json at configure time, so a configure-only
        CMake tree already answers every question about flags and macros.  A
        configured Autotools tree holds the same information in its generated
        makefiles, and `make -n` is how you read it out: the commands are printed
        and nothing is run.

        This is what the `probe` module needs.  It configures under a compiler
        that rejects `-mavx512f` and asks which flags reached the AVX-512 unit --
        a question about the configuration, not about a compilation, and one that
        would otherwise cost a full build per probe.
        """
        if self.is_cmake or not self.configured:
            return []
        cached = self.steps.get("dryrun")
        if cached is None:
            cached = run("dryrun", ["make", "-n", "V=1"], self.bld,
                         CONFIGURE_TIMEOUT, self.env)
            self.steps["dryrun"] = cached
        entries = self._scan_transcript(cached.out)
        return entries or self._scan_transcript(cached.out, wrappers=True)

    def verbose_compile_lines(self):
        """Every real compiler invocation in the verbose build transcript.

        Deduplicated by source, for the same reason the recovered database is:
        libtool compiles each unit twice, and the check that reads this counts
        invocations against the 119 translation units State A has.  A transcript
        of 238 lines covering 119 units answers "119", not "238".
        """
        return [line for _path, line in self._recovered_compile_commands()]

    def macro_map(self):
        """{MACRO: value|None} defined for library translation units.

        Union over every compile command whose source lives under src/libsodium,
        which is what "delivered to every library translation unit" means.
        """
        per_file = self.macros_per_source()
        if not per_file:
            return {}
        common = None
        for macros in per_file.values():
            keys = set(macros)
            common = keys if common is None else (common & keys)
        first = next(iter(per_file.values()))
        return {k: first.get(k) for k in sorted(common or ())}

    def macros_per_source(self):
        """{source_relpath: {MACRO: value|None}} for library sources."""
        res = {}
        entries = self.compile_commands()
        if not entries:
            for line in self.verbose_compile_lines():
                m = re.search(r"(\S+\.c)\b", line)
                if not m:
                    continue
                entries.append((m.group(1), line))
        for path, cmd in entries:
            norm = path.replace("\\", "/")
            if "/src/libsodium/" not in norm:
                continue
            rel = norm.split("/src/libsodium/", 1)[1]
            res[rel] = parse_defines(cmd)
        return res

    def all_flags_per_source(self):
        """{source_relpath: raw compile command} for library sources.

        Unlike flags_per_source, which isolates the -m ISA flags, this keeps the
        whole command line so the hardening suite can look for optimisation,
        visibility and fortification spellings wherever they appear.
        """
        res = {}
        for path, cmd in self.compile_commands():
            norm = path.replace("\\", "/")
            if "/src/libsodium/" not in norm:
                continue
            res[norm.split("/src/libsodium/", 1)[1]] = cmd
        return res

    def flags_per_source(self):
        """{source_relpath: [compiler flags]} for library sources."""
        res = {}
        for path, cmd in self.compile_commands():
            norm = path.replace("\\", "/")
            if "/src/libsodium/" not in norm:
                continue
            rel = norm.split("/src/libsodium/", 1)[1]
            res[rel] = re.findall(r"(?:^|\s)(-m[\w.=+-]+)", cmd)
        return res

    # ------------------------------------------------------------ the tests --
    #
    # Three questions, asked of whichever runner the tree ships: which tests are
    # registered, did the run come back clean, and which test got which verdict.
    # `ctest -N` / `ctest` answer them for CMake; automake's TESTS variable and
    # `make check` answer them for Autotools.  The names below stay `ctest_*`
    # because that is what the module reading them is called and what §1.8 asks
    # for -- the port has to end up with CTest.
    def ctest_list(self):
        return (self._ctest_list() if self.is_cmake else self._automake_list())

    def _ctest_list(self):
        s = self.steps.get("ctest_list")
        if s is None:
            s = run("ctest_list", ["ctest", "-N"], self.bld, 300)
            self.steps["ctest_list"] = s
        names = []
        for line in s.out.splitlines():
            m = re.match(r"\s*Test\s+#\d+:\s+(\S+)\s*$", line)
            if m:
                names.append(m.group(1))
        return names

    def test_subdir(self):
        """Where in the build tree the test suite's Makefile is."""
        for rel in ("test/default", "test", "tests/default", "tests"):
            if os.path.isfile(os.path.join(self.bld, rel, "Makefile")):
                return os.path.join(self.bld, rel)
        return None

    def _automake_list(self):
        """The registered tests, asked of make without running them.

        automake's `TESTS` is exactly the CTest registry: the list `make check`
        will run, computed by configure and conditional on the options it was
        given.  Reading it costs nothing and, like `ctest -N`, says what is
        registered rather than what happens to pass.

        Asked for with a second makefile supplying one extra goal, because
        `TESTS` is a computed variable -- grepping the Makefile for it yields
        `$(am__EXEEXT_3)`, not eighty names.
        """
        d = self.test_subdir()
        if d is None:
            return []
        s = self.steps.get("ctest_list")
        if s is None:
            helper = os.path.join(self.root, "srb-print-tests.mk")
            try:
                with open(helper, "w") as fh:
                    fh.write("srb-print-tests:\n\t@echo $(TESTS)\n")
            except OSError:
                return []
            s = run("ctest_list",
                    ["make", "-s", "--no-print-directory", "-f", "Makefile",
                     "-f", helper, "srb-print-tests"],
                    d, 300, self.env)
            self.steps["ctest_list"] = s
        if not s.ok:
            return []
        names = []
        for line in s.out.splitlines():
            if line.startswith("make") or ":" in line:
                continue        # a diagnostic, not the variable
            names.extend(t for t in line.split() if t)
        return names

    def registered_commands(self):
        """{test_name: argv} -- what the runner will actually execute.

        The question behind it is "is this test a compiled program or a stub":
        `add_test(NAME auth COMMAND true)` registers a name and runs nothing, and
        the only way to tell is to look at the command.  CTest publishes it as
        JSON; automake's `TESTS` names the program, and the command is the
        executable that name resolves to in the test build directory -- through a
        libtool wrapper where there is one, since the wrapper is the build system's
        own indirection and not the test's.

        A test whose binary is not on disk is reported with an empty argv rather
        than omitted: "registered but never built" is exactly what the checks
        reading this are looking for, and dropping it would hide it.
        """
        return (self._ctest_commands() if self.is_cmake
                else self._automake_commands())

    def _ctest_commands(self):
        s = self.steps.get("ctest_json")
        if s is None:
            s = run("ctest_json", ["ctest", "--show-only=json-v1"], self.bld, 300)
            self.steps["ctest_json"] = s
        try:
            doc = json.loads(s.out)
        except ValueError:
            return {}
        out = {}
        for t in doc.get("tests", []):
            out[t.get("name")] = list(t.get("command") or [])
        return out

    def _automake_commands(self):
        """`TESTS` names programs, so the command is the program.

        automake builds the test programs under `make check` and not under
        `make`, so the binaries only exist once the suite has been run.  That is
        arranged for here rather than left to the caller: `make check TESTS=` (an
        empty goal list) compiles `check_PROGRAMS` and runs nothing, which is the
        cheapest way to have the artefacts the caller is about to inspect.

        The name in `TESTS` is not necessarily the executable.  A program linked
        against an uninstalled shared library gets a libtool wrapper: `test/default/
        auth` is a generated shell script whose job is to set the library path and
        `exec "$progdir/$program"`, and the ELF it execs is `test/default/.libs/auth`.
        So the wrapper is followed, because the caller's question is what the
        registered test *runs* and the answer is the ELF behind it -- the same
        answer `ctest --show-only` gives directly for a CMake tree, where
        `add_test` names the binary and there is no wrapper.  A name that resolves
        to neither is reported with an empty argv rather than omitted: "registered
        but never built" is exactly what the checks reading this look for.
        """
        d = self.test_subdir()
        if d is None:
            return {}
        names = self._automake_list()
        if names and not any(os.path.isfile(os.path.join(d, n)) for n in names):
            if self.steps.get("check_programs") is None:
                self.steps["check_programs"] = run(
                    "check_programs",
                    ["make", "check", "TESTS=", "-j", str(nproc())],
                    d, CTEST_TIMEOUT, self.env)
        out = {}
        for n in names:
            p = self._resolve_test_program(d, n)
            out[n] = [p] if p else []
        return out

    @staticmethod
    def _resolve_test_program(directory, name):
        """The executable `name` stands for in `directory`, or None if it is absent.

        An ELF at the named path is the program.  Otherwise, if that path is a
        libtool wrapper -- identified by the marker libtool writes into the script
        it generates, not by the path merely being a script, because a test
        registered as a hand-written shell script is a finding and must be
        reported as what it is -- the ELF it execs out of `.libs/` is.
        """
        p = os.path.join(directory, name)
        if not os.path.isfile(p):
            return None
        try:
            with open(p, "rb") as fh:
                head = fh.read(4096)
        except OSError:
            return None
        if head.startswith(b"\x7fELF"):
            return p
        if b"Generated by libtool" not in head:
            return p                    # not a wrapper: report what is registered
        real = os.path.join(directory, ".libs", name)
        try:
            with open(real, "rb") as fh:
                if fh.read(4).startswith(b"\x7fELF"):
                    return real
        except OSError:
            pass
        return p

    def source_package(self, timeout=INSTALL_TIMEOUT):
        """Produce a source archive, and say what was run and what came out.

        Both build systems have the capability §1.9 names -- `make dist` is where
        an Autotools project puts it and CPack's source generator is where a CMake
        one does -- so the archive is what the checks read, and how it was asked
        for is this method's business.

        Returns {"step", "config", "archives"}; `config` is the
        CPackSourceConfig.cmake path, which only a CMake tree has.
        """
        cached = self.steps.get("_srcpkg")
        if cached is not None:
            return cached
        cfg = os.path.join(self.bld, "CPackSourceConfig.cmake")
        if self.is_cmake:
            attempts = []
            if os.path.isfile(cfg):
                attempts.append(["cpack", "--config", cfg])
            attempts.append(["cmake", "--build", self.bld,
                             "--target", "package_source"])
        else:
            attempts = [["make", "dist"]]
        last = None
        for argv in attempts:
            last = run("package_source", argv, self.bld, timeout, self.env)
            self.steps["package_source"] = last
            if last.ok:
                break
        archives = []
        for base in (self.bld, os.path.join(self.bld, "..")):
            if not os.path.isdir(base):
                continue
            for f in sorted(os.listdir(base)):
                if re.search(r"\.(tar\.gz|tar\.bz2|tar\.xz|tgz|zip)$", f) and \
                        "libsodium" in f.lower():
                    archives.append(os.path.join(base, f))
        out = {"step": last, "config": cfg, "archives": archives}
        self.steps["_srcpkg"] = out
        return out

    #: Cache types a capability probe leaves behind, not a control anyone sets.
    PROBE_CACHE_TYPES = ("INTERNAL", "STATIC")

    def feature_switch(self, *patterns):
        """The delivered build system's own name for an optional feature.

        §1.10 requires `--disable-ssp` to keep working without saying what the
        port must call its replacement, so the switch is looked up rather than
        assumed: a cache entry matching one of `patterns` for a CMake tree, an
        `--enable-`/`--disable-` pair in `configure --help` for an Autotools one.
        Returns (name, value_to_turn_it_off) or None when the delivered build
        system has no such control, which is a fact about it and not a defect.

        Patterns are tried in order and the first that matches anything wins,
        which is the same rule the Autotools branch below already followed.  It
        is load-bearing: a caller passes a precise pattern and then a broad one,
        and a single pass over a sorted cache answers with whichever *name*
        sorted first instead of with whichever *pattern* was meant.  That is how
        `feature_switch(r"SSP", r"STACK")` came back with
        SODIUM_ACCEPTS_NOEXECSTACK, a compiler-capability probe that sorts before
        SODIUM_USE_SSP, and turned a check about the stack protector into a check
        about something else that then failed.
        """
        if self.is_cmake:
            cache = self.cmake_cache()
            for p in patterns:
                for name in sorted(cache):
                    if cache[name][0] in self.PROBE_CACHE_TYPES:
                        continue
                    if re.search(p, name):
                        return (name, "OFF")
            return None
        help_text = self.configure_help()
        for p in patterns:
            m = re.search(r"^  --(?:enable|disable)-(\S*%s\S*)" % p, help_text,
                          re.M | re.I)
            if m:
                return ("--disable-" + m.group(1), None)
        return None

    def ctest_run(self, parallel=True, timeout=CTEST_TIMEOUT):
        key = "ctest_run_%s" % ("par" if parallel else "seq")
        s = self.steps.get(key)
        if s is None:
            s = (self._ctest_invoke(key, parallel, timeout) if self.is_cmake
                 else self._make_check(key, parallel, timeout))
            self.steps[key] = s
        return s

    def _ctest_invoke(self, key, parallel, timeout):
        argv = ["ctest", "--output-on-failure"]
        if parallel:
            argv += ["-j", str(nproc())]
        return run(key, argv, self.bld, timeout)

    def _make_check(self, key, parallel, timeout):
        """`make check`, with the previous run's verdicts cleared first.

        automake records each test's verdict in a `.trs` file and skips a test
        whose `.trs` is newer than its binary.  A second `make check` would
        therefore print nothing and report success, which is the wrong answer to
        "did the tests pass when run this way" -- the parallel-vs-serial
        comparison exists precisely to run them twice.
        """
        d = self.test_subdir() or self.bld
        for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if name.endswith((".trs", ".log")):
                try:
                    os.unlink(os.path.join(d, name))
                except OSError:
                    pass
        argv = ["make", "check"]
        if parallel:
            argv += ["-j", str(nproc())]
        return run(key, argv, d, timeout, self.env)

    #: automake's per-test verdict line, e.g. `PASS: sodium_utils`.  XFAIL is an
    #: expected failure and XPASS an unexpected pass; libsodium declares neither,
    #: and both are counted as the test not having gone the way State A's did.
    TRS_RE = re.compile(r"^(PASS|FAIL|SKIP|XFAIL|XPASS|ERROR):\s+(\S+)")

    def ctest_results(self, parallel=True):
        """{test_name: passed?} for the run."""
        s = self.ctest_run(parallel=parallel)
        res = {}
        if self.is_cmake:
            for line in s.out.splitlines():
                m = re.match(r"\s*\d+/\d+\s+Test\s+#\d+:\s+(\S+)\s+\.*\s*(\S+)",
                             line)
                if m:
                    res[m.group(1)] = m.group(2).startswith("Passed")
            return res
        for line in s.out.splitlines():
            m = self.TRS_RE.match(line)
            if m:
                res[m.group(2)] = m.group(1) == "PASS"
        return res


# --------------------------------------------------------------------------- #
# Crossing a process boundary
#
# The matrix is built once, in the `build` module.  Every other module that reads
# it is a separate process, and re-running the matrix in each would cost a whole
# matrix per module instead of one in total.  So the build trees stay on
# disk under $SRB_SUITE_WORK and this ledger records how to talk about them:
# which configuration a directory is, what argv produced it, what each step
# returned, and where its full output was written.
#
# Output goes to files rather than into the ledger because the checks that read a
# build log search it -- for "undefined reference", for a fetch, for a shell
# invocation -- and a needle elided from the middle of a clipped string reads as a
# clean build.
# --------------------------------------------------------------------------- #

LEDGER_SCHEMA = "swerefactor-build01-builds-v1"


def persist(builds, state_dir):
    """Write every build's streams to disk and return a JSON-safe ledger."""
    os.makedirs(state_dir, exist_ok=True)
    ledger = {"schema": LEDGER_SCHEMA, "builds": {}}
    for name, build in sorted(builds.items()):
        logs = os.path.join(state_dir, "logs-" + name)
        os.makedirs(logs, exist_ok=True)
        record = {
            "name": build.name,
            "kwargs": {
                "generator": build.generator,
                "options": dict(build.options),
                "build_type": build.build_type,
                "compiler": build.compiler,
                "configure_only": build.configure_only,
                "extra_env": dict(build.extra_env),
                "export_cc": build.export_cc,
                "root": build.root,
            },
            "ran": build.ran,
            # Recorded rather than re-detected: the reader would look at the same
            # source copy and agree, but only while it is still on disk, and a
            # restored Build that disagreed with the one that ran would drive the
            # wrong test runner.
            "flavour": build.flavour,
            "steps": {},
        }
        for key, step in sorted(build.steps.items()):
            out_path = os.path.join(logs, key.replace("/", "_") + ".out")
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

    The build trees themselves are untouched on disk, so an object restored here
    answers install_entries(), compile_commands() and the rest by reading the same
    directories the build wrote -- it is the recorded *steps* that cannot be
    recovered from the filesystem, which is what the ledger is for.
    """
    if ledger.get("schema") != LEDGER_SCHEMA:
        raise ValueError("not a build ledger: schema=%r" % ledger.get("schema"))
    out = {}
    for name, record in (ledger.get("builds") or {}).items():
        build = Build(record["name"], **record["kwargs"])
        build.ran = bool(record.get("ran"))
        if record.get("flavour") in (CMAKE, AUTOTOOLS):
            build._flavour = record["flavour"]
        for key, step in (record.get("steps") or {}).items():
            try:
                with open(step["out_path"], encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError as exc:
                text = "*** the recorded output is unreadable: %s ***" % exc
            build.steps[key] = Step(key, list(step["argv"]), int(step["rc"]), text,
                                    float(step["seconds"]),
                                    bool(step["timed_out"]),
                                    started=float(step["started"]))
        out[name] = build
    return out


def parse_defines(cmd: str):
    """Extract -D macros from a compile command string."""
    out = {}
    for m in re.finditer(r'(?:^|\s)-D([A-Za-z_][A-Za-z0-9_]*)(?:=("(?:[^"\\]|\\.)*"|\S+))?', cmd):
        name, val = m.group(1), m.group(2)
        if val is not None:
            val = val.strip()
            if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                val = val[1:-1].replace('\\"', '"')
            val = val.replace('\\"', '"')
        out[name] = val
    return out
