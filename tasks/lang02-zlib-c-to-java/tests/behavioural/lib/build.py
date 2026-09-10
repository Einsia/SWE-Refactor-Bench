#!/usr/bin/env python3
"""Builds: the submission, the pinned C reference, and the verifier's own tools.

Three things get built in a run of this verifier, and they are built in three
different ways on purpose.

The **submission** is built exactly as the build contract publishes it -- `cmake
-S . -B build && cmake --build build && cmake --install build`, twice, once per
configuration -- with the C and C++ compilers replaced by a shim that refuses to
compile anything in the repository.  The shim is what makes "the C is gone" a
build-time fact instead of a promise: a submission that kept one translation unit
fails to build rather than failing a case somewhere downstream.  CMake remains
the driver even though the product is now a jar, because the alternative
(switching to Maven or Gradle) needs the network for plugin resolution and would
make this a build-system migration rather than a language port.

The **reference** is the pinned C zlib, built from the same tarball the agent
received, with a real compiler, in this image.  It is the oracle: every expected
value in the behavioural suite is computed from it rather than transcribed, so the
reference's own quirks -- which bytes a level emits, what `example` prints, how
`gzprintf` rounds -- are the specification by construction.

The verifier's **own tools** are the two halves of the differential pair.
`probe.c` is compiled against the reference install with `cc`; `Probe.java` is
compiled against the submission's installed jar with `javac`.  Neither is the
submission's code and neither is built with the shim on PATH.

Java-specific note on where the JDK comes from.  Every javac, jar and java
invocation resolves through `java_tool()`, which reads JAVA_HOME rather than
PATH.  The image has exactly one JDK, so today the two agree; going through
JAVA_HOME anyway means a second JDK arriving on the image -- or a submission
prepending one to PATH during its build -- cannot quietly become the toolchain a
submission is graded with.  The bytecode major version is a graded fact, and a
graded fact must not depend on search order.
"""

from __future__ import annotations

import collections
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import vlib
from vlib import Log, Result

CONFIGURE_TIMEOUT = 900.0
BUILD_TIMEOUT = 2700.0
INSTALL_TIMEOUT = 600.0
JAVAC_TIMEOUT = 600.0
JAVA_RUN_TIMEOUT = 120.0

# Every driver name a C or C++ compile could plausibly arrive through.  The
# assembler, archiver and linker are absent for the same reason they were absent
# in the Rust form of this task: the shim must stop the repository being
# compiled, not stop the toolchain functioning.  javac and jar are of course not
# shimmed -- they are how State B is built.
SHIM_TOOLS = (
    "cc", "gcc", "gcc-12", "c++", "g++", "g++-12", "clang", "clang++", "cpp",
    "x86_64-linux-gnu-gcc", "x86_64-linux-gnu-gcc-12", "x86_64-linux-gnu-g++",
    "x86_64-linux-gnu-g++-12",
)

REAL_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# The JDK the contract pins.  Read from the environment when set so the harness
# can move it, defaulted so a bare `python3 verify.py` on the image still works.
JAVA_HOME = Path(os.environ.get("JAVA_HOME", "/usr/lib/jvm/java-17-openjdk-amd64"))

# `javac --release 17`, from jvm_code_policy.  Passed explicitly to every
# verifier-side compile so the probe and the submission are held to one language
# level: a probe accidentally compiled at 21 could use a construct the submission
# is forbidden from having to understand.
JAVA_RELEASE = "17"

# The module the submission delivers, and the jar name the install contract
# publishes.  Both are duplicated in source-contract.json; these are the
# fallbacks used by helpers that run before the contract is loaded.
MODULE_NAME = "org.zlib"
JAR_RELPATH = "share/java/zlib-1.3.1.jar"

# The configurations the build contract publishes.  The ids match the `configs`
# column of the catalog's struct cases.  Both are graded, and BUILD_SHARED_LIBS
# is meaningless for a jar by design: an option a build has no use for must
# still be accepted, because refusing it breaks the build interface downstreams
# depend on.
CONFIGS = ("static", "shared")

# Upstream's own side effect on the source tree, listed so a guard can tell it
# apart from a submission editing its own repository during the build.  zlib's
# CMakeLists renames zconf.h out of the way at configure time; State A does it
# too, so it is expected rather than suspicious.
EXPECTED_SOURCE_EFFECTS = ("zconf.h", "zconf.h.included")


# `target:` at column 0.  Not `VAR := ...`, not a comment, not a `.SPECIAL`.
_MAKE_RULE = re.compile(r"^([^\s:#=][^:=]*):(?!=)")

# make's own boilerplate, declared in every generated build.make with no recipe.
_MAKE_SPECIALS = frozenset({"cmake_force", "%", "clean"})


def recipe_owners(build_dir: Path) -> dict[str, set[str]]:
    """Which generated targets carry a *recipe* for each output.

    The Makefile generator writes one `build.make` per target.  When two targets
    each `DEPENDS` on the same `add_custom_command` OUTPUT, CMake has no target to
    attach that command to, so it copies the recipe into *both* -- and `make -j`
    is then free to run it twice at once.  See `graded_build_is_parallel` in the
    task's instruction.md for the contract this measures.

    Counting bare rules does not find it: every `build.make` declares `.PHONY`,
    `.SUFFIXES`, `cmake_force` and `%`.  Those are declarations with no recipe.
    What matters is a recipe -- a rule whose next non-blank line is tab-indented.

    Reads the generated build system rather than the outcome of a build, so the
    answer does not depend on whether the race happened to fire this time.
    Returns {} for a generator that emits no `build.make` (Ninja), which is not a
    finding: this hazard is the Makefile generator's.
    """
    owners: dict[str, set[str]] = collections.defaultdict(set)
    for path in sorted(build_dir.glob("CMakeFiles/*/build.make")):
        target = path.parent.name
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            match = _MAKE_RULE.match(line)
            if not match:
                continue
            out = match.group(1).strip()
            if out.startswith(".") or out in _MAKE_SPECIALS:
                continue
            for nxt in lines[i + 1:]:
                if nxt.startswith("\t"):
                    owners[out].add(target)
                    break
                if not nxt.strip() or _MAKE_RULE.match(nxt):
                    break
    return owners


def duplicated_recipes(build_dir: Path) -> dict[str, set[str]]:
    """The outputs built by more than one target: {output: {target, target}}."""
    return {o: t for o, t in recipe_owners(build_dir).items() if len(t) > 1}


def java_tool(name: str) -> str:
    """Absolute path to a JDK tool, via JAVA_HOME rather than PATH.

    Falls back to the bare name only when JAVA_HOME does not contain the tool,
    which on a correctly built image does not happen -- and when it does, letting
    the call fail loudly with a real path in the log beats silently grading
    against whatever `java` PATH resolved to.
    """
    candidate = JAVA_HOME / "bin" / name
    if candidate.is_file():
        return str(candidate)
    return name


@dataclass
class BuildOutcome:
    """What happened in one configuration."""

    config: str
    build_dir: Path
    prefix: Path
    configure: Result | None = None
    compile: Result | None = None
    install: Result | None = None
    extra: dict[str, Result] = field(default_factory=dict)
    shim_log: Path | None = None
    # Where the probes put their alternate build and install trees.  Carried on
    # the outcome so the cases that grade those trees read the same path the
    # probe wrote, instead of both sides being told separately.
    scratch: Path | None = None

    @property
    def configured(self) -> bool:
        return self.configure is not None and self.configure.ok

    @property
    def built(self) -> bool:
        return self.compile is not None and self.compile.ok

    @property
    def installed(self) -> bool:
        return self.install is not None and self.install.ok

    @property
    def jar(self) -> Path:
        """The delivered artifact in this configuration's install tree."""
        return self.prefix / JAR_RELPATH

    def summary(self) -> dict:
        payload = {
            "config": self.config,
            "build_dir": str(self.build_dir),
            "prefix": str(self.prefix),
            "configured": self.configured,
            "built": self.built,
            "installed": self.installed,
        }
        for name, result in (
            ("configure", self.configure),
            ("compile", self.compile),
            ("install", self.install),
        ):
            if result is not None:
                payload[name] = result.brief(limit=8000)
        for name, result in sorted(self.extra.items()):
            payload[f"probe_{name}"] = result.brief(limit=2000)
        return payload

    # -- crossing a process boundary --------------------------------------
    #
    # The build runs in its own module and the checks that read its logs run in
    # others, so the outcome has to survive being written down.  summary() is the
    # report and clips each stream; these two are the handover and do not, because
    # check_build_warnings() searches the compile log for javac's own diagnostics
    # and a needle elided from the middle of a clipped log would read as a clean
    # build.
    #
    # scratch is carried across as well.  The five build probes write their
    # alternate build and install trees under it, and the cases that grade those
    # trees find them by asking the outcome where they are; a restored outcome
    # that had forgotten would send those cases looking in a directory that the
    # probe never wrote.

    def persist(self, state_dir: Path) -> dict:
        """Write every captured stream to disk and return a path-only record."""
        logs = state_dir / f"logs-{self.config}"
        logs.mkdir(parents=True, exist_ok=True)
        record: dict = {
            "config": self.config,
            "build_dir": str(self.build_dir),
            "prefix": str(self.prefix),
            "shim_log": str(self.shim_log) if self.shim_log else "",
            "scratch": str(self.scratch) if self.scratch else "",
            "steps": {},
        }
        steps: list[tuple[str, Result | None]] = [
            ("configure", self.configure),
            ("compile", self.compile),
            ("install", self.install),
        ]
        steps += [(f"extra:{name}", res) for name, res in sorted(self.extra.items())]
        for name, result in steps:
            if result is None:
                continue
            stem = name.replace(":", "-")
            out = logs / f"{stem}.out"
            err = logs / f"{stem}.err"
            out.write_bytes(result.stdout)
            err.write_bytes(result.stderr)
            record["steps"][name] = {
                "argv": result.argv,
                "cwd": result.cwd,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "duration": result.duration,
                "stdout_path": str(out),
                "stderr_path": str(err),
            }
        return record

    @classmethod
    def restore(cls, record: dict) -> "BuildOutcome":
        """Rebuild an outcome from persist(), reading the streams back in full."""
        outcome = cls(
            config=record["config"],
            build_dir=Path(record["build_dir"]),
            prefix=Path(record["prefix"]),
            shim_log=Path(record["shim_log"]) if record.get("shim_log") else None,
            scratch=Path(record["scratch"]) if record.get("scratch") else None,
        )
        for name, step in (record.get("steps") or {}).items():
            result = Result(
                argv=list(step["argv"]),
                cwd=step["cwd"],
                returncode=int(step["returncode"]),
                stdout=_read_bytes(step["stdout_path"]),
                stderr=_read_bytes(step["stderr_path"]),
                duration=float(step["duration"]),
                timed_out=bool(step["timed_out"]),
            )
            if name.startswith("extra:"):
                outcome.extra[name.split(":", 1)[1]] = result
            else:
                setattr(outcome, name, result)
        return outcome


def _read_bytes(path: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError:
        return b""


def install_shim(shim_src: Path, shim_dir: Path, log: Log) -> Path:
    """Materialize the compiler shim under every driver name it must intercept."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    target = shim_dir / "ccshim.py"
    shutil.copy2(shim_src, target)
    target.chmod(0o755)
    for tool in SHIM_TOOLS:
        link = shim_dir / tool
        if link.exists() or link.is_symlink():
            link.unlink()
        # A symlink, not a wrapper script: the shim reads argv[0] to learn which
        # driver was asked for, and a `sh -c exec python3 ccshim.py` wrapper
        # would replace that with the shim's own name.  The shebang makes the
        # symlink directly executable while argv[0] stays the tool name.
        link.symlink_to(target.name)
    log.write(f"shim: installed {len(SHIM_TOOLS)} wrappers in {shim_dir}")
    return target


def shim_env(shim_dir: Path, shim_log: Path, repo: Path, **overrides) -> dict:
    """An environment in which repository C and C++ cannot be compiled.

    JAVA_HOME is exported because the submission's CMakeLists is expected to find
    javac through it -- `find_package(Java)` and a bare `javac` both work, but the
    contract names JAVA_HOME, so the graded build gets it.  The JDK's own bin
    directory is deliberately *not* prepended to PATH: a submission that only
    builds because the harness put javac first on PATH has a CMakeLists that
    would not build for a downstream, and finding the compiler is part of what a
    build system is for.
    """
    env = vlib.base_env(
        PATH=f"{shim_dir}:{REAL_PATH}",
        CCSHIM_PATH=REAL_PATH,
        CCSHIM_LOG=str(shim_log),
        CCSHIM_REPO=str(repo),
        JAVA_HOME=str(JAVA_HOME),
        CMAKE_BUILD_PARALLEL_LEVEL="4",
    )
    env.update({k: v for k, v in overrides.items() if v is not None})
    return env


class Builder:
    """Drives one submission build in one published configuration."""

    def __init__(
        self, repo: Path, workspace: Path, shim_dir: Path, log: Log, *, config: str
    ) -> None:
        self.repo = repo
        self.config = config
        self.log = log
        self.build_dir = workspace / f"build-{config}"
        self.prefix = workspace / f"install-{config}"
        self.shim_dir = shim_dir
        self.shim_log = workspace / f"shim-{config}.jsonl"
        # The probes write their alternate build and install trees here, and the
        # structural cases read those same trees back.  Deriving it from the
        # workspace rather than taking it as a probe argument is deliberate: the
        # two sides have to agree on the path, and when the caller passed it
        # separately to each they could -- and did -- disagree silently, leaving
        # the cases reporting a directory that "was not created" when in fact it
        # had been created somewhere else.
        self.scratch = workspace / "scratch"
        self.outcome = BuildOutcome(
            config=config,
            build_dir=self.build_dir,
            prefix=self.prefix,
            shim_log=self.shim_log,
            scratch=self.scratch,
        )

    @property
    def shared_flag(self) -> str:
        return "ON" if self.config == "shared" else "OFF"

    def env(self, **overrides) -> dict:
        return shim_env(self.shim_dir, self.shim_log, self.repo, **overrides)

    def configure_argv(self, build_dir: Path, prefix: Path) -> list[str]:
        # Exactly the argv published in the build contract.  The install prefix is
        # fixed here and not changed later: a build that bakes the prefix into
        # what it installs -- a wrapper script naming the jar by absolute path,
        # for instance -- must bake in the prefix it is actually installed to.
        #
        # ZLIB_BUILD_EXAMPLES is passed explicitly at its upstream default of ON.
        # Naming it rather than relying on the default makes the graded build's
        # expectation visible: the two test drivers are part of what has to build,
        # and a submission cannot pass by quietly defaulting them off.
        return [
            "cmake",
            "-S", ".",
            "-B", str(build_dir),
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DBUILD_SHARED_LIBS={self.shared_flag}",
            "-DZLIB_BUILD_EXAMPLES=ON",
            f"-DCMAKE_INSTALL_PREFIX={prefix}",
        ]

    def configure(self) -> Result:
        self.prefix.mkdir(parents=True, exist_ok=True)
        result = vlib.run(
            self.configure_argv(self.build_dir, self.prefix),
            cwd=self.repo,
            env=self.env(),
            timeout=CONFIGURE_TIMEOUT,
            log=self.log,
            label=f"configure-{self.config}",
        )
        self.outcome.configure = result
        return result

    def compile(self) -> Result:
        result = vlib.run(
            ["cmake", "--build", str(self.build_dir), "--parallel"],
            cwd=self.repo,
            env=self.env(),
            timeout=BUILD_TIMEOUT,
            log=self.log,
            label=f"build-{self.config}",
        )
        self.outcome.compile = result
        return result

    def install(self) -> Result:
        result = vlib.run(
            ["cmake", "--install", str(self.build_dir)],
            cwd=self.repo,
            env=self.env(),
            timeout=INSTALL_TIMEOUT,
            log=self.log,
            label=f"install-{self.config}",
        )
        self.outcome.install = result
        return result

    def run_all(self) -> BuildOutcome:
        self.log.section(f"build submission ({self.config})")
        if not self.configure().ok:
            self.log.write(f"configure failed ({self.config}); skipping build")
            return self.outcome
        if not self.compile().ok:
            self.log.write(f"build failed ({self.config}); skipping install")
            return self.outcome
        self.install()
        return self.outcome

    # -- supplementary structural probes ---------------------------------
    #
    # These run after the graded build has produced its install tree, so a failure
    # here costs the cases it belongs to and nothing else.  They also get a
    # shorter build timeout than the graded build: that build has already
    # demonstrated the project compiles, so a probe that cannot finish in a
    # fraction of the same budget is reporting a problem rather than needing more
    # time.
    PROBE_BUILD_TIMEOUT = 900.0

    def _jar_stamp(self, prefix: Path | None = None) -> tuple[float, int]:
        """(mtime, size) of the installed jar, or (0, -1) when it is absent."""
        jar = (prefix or self.prefix) / JAR_RELPATH
        try:
            info = jar.stat()
        except OSError:
            return 0.0, -1
        return info.st_mtime, info.st_size

    def probe_rebuild(self) -> Result:
        """A second build over a finished tree must stay green and do nothing.

        For a C project the tell was recompilation; for a jar it is the jar's own
        timestamp.  A build that re-runs javac and rewrites the artifact every
        time has usually declared its custom command with no OUTPUT or no DEPENDS,
        which means CMake has no idea what the build produces -- and a build system
        that cannot say what it produces cannot be depended on by anything that
        consumes it.  So the stamp is taken before and after, and the case that
        consumes this probe reads the comparison rather than parsing the log.
        """
        before = self._jar_stamp()
        result = vlib.run(
            ["cmake", "--build", str(self.build_dir)],
            cwd=self.repo,
            env=self.env(),
            timeout=self.PROBE_BUILD_TIMEOUT,
            log=self.log,
            label=f"rebuild-{self.config}",
        )
        self.outcome.extra["rebuild"] = result
        # Only the *build* is re-run, not the install, so this compares the jar as
        # the first install left it.  A rebuild that rewrote the build-tree jar but
        # did not reinstall would be caught by the build-tree stamp instead, which
        # is why both are recorded.
        after = self._jar_stamp()
        self.outcome.extra["rebuild-jar-stable"] = vlib.synthetic(
            "rebuild:jar-stable",
            before == after and before[1] >= 0,
            f"installed jar {'unchanged' if before == after else 'rewritten'} "
            f"by the second build (before={before}, after={after})",
        )
        return result

    def probe_reconfigure(self) -> Result:
        """Re-running configure over a populated build directory must succeed.

        This is also where upstream's zconf.h rename gets exercised a second time:
        the first configure moved the file away, so a submission that re-creates
        it, or that fails when it is missing, breaks here.  The rename survives the
        port because CMakeLists survives the port -- and a submission that removed
        the rename along with the C would break a downstream that adds zlib as a
        subdirectory and relies on the source tree not being polluted.
        """
        result = vlib.run(
            self.configure_argv(self.build_dir, self.prefix),
            cwd=self.repo,
            env=self.env(),
            timeout=CONFIGURE_TIMEOUT,
            log=self.log,
            label=f"reconfigure-{self.config}",
        )
        self.outcome.extra["reconfigure"] = result
        return result

    def probe_outofsource(self, scratch: Path | None = None) -> Result:
        """A full build in a fresh directory outside the source tree.

        The C form of this probe only configured, because what it was checking was
        that two files got *generated* into the build directory instead of being
        hardcoded in the source tree.  The Java form has to build, because the
        thing that must land outside the source tree is now the whole product: the
        jar and the two driver scripts.  A submission that writes its classes next
        to its sources, or that stages a jar committed to the repository, produces
        a build directory with nothing in it -- and would still pass a
        configure-only probe.

        The artifacts are recorded before the directory is removed, so the case
        that consumes this reads what was found rather than re-deriving it.
        """
        scratch = self.scratch if scratch is None else scratch
        out = scratch / f"oos-{self.config}"
        shutil.rmtree(out, ignore_errors=True)
        result = vlib.run(
            self.configure_argv(out, self.prefix),
            cwd=self.repo,
            env=self.env(),
            timeout=CONFIGURE_TIMEOUT,
            log=self.log,
            label=f"outofsource-configure-{self.config}",
        )
        self.outcome.extra["outofsource"] = result
        if result.ok:
            built = vlib.run(
                ["cmake", "--build", str(out), "--parallel"],
                cwd=self.repo,
                env=self.env(),
                timeout=self.PROBE_BUILD_TIMEOUT,
                log=self.log,
                label=f"outofsource-build-{self.config}",
            )
            self.outcome.extra["outofsource-build"] = built
            jars = sorted(p.name for p in out.rglob("*.jar"))
            self.outcome.extra["outofsource-jar"] = vlib.synthetic(
                "outofsource:jar", bool(jars),
                f"jar(s) produced in the out-of-source tree: {jars[:4] or 'none'}",
            )
            for name in ("example", "minigzip"):
                found = [p for p in out.rglob(name) if p.is_file()]
                self.outcome.extra[f"outofsource-{name}"] = vlib.synthetic(
                    f"outofsource:{name}", bool(found),
                    f"{name} {'produced in' if found else 'missing from'} {out}",
                )
        shutil.rmtree(out, ignore_errors=True)
        return result

    def probe_altprefix(self, scratch: Path | None = None) -> tuple[Result, Path]:
        """A full build and install under a different prefix.

        CMAKE_INSTALL_PREFIX has to place everything under the prefix given, with
        nothing escaping to an absolute path baked in at configure time.  The Java
        form has a new way to get this wrong that the C form did not: the driver
        contract requires an exec'able wrapper script, and the obvious way to write
        one is to interpolate the jar's absolute path into it.  That is fine --
        expected, even -- but it means the script is prefix-dependent, so a
        submission that generates it once and reuses it across configures produces
        a tree whose drivers point at somewhere else's jar.
        """
        scratch = self.scratch if scratch is None else scratch
        build = scratch / f"altbuild-{self.config}"
        prefix = scratch / f"altprefix-{self.config}"
        shutil.rmtree(build, ignore_errors=True)
        shutil.rmtree(prefix, ignore_errors=True)
        prefix.mkdir(parents=True, exist_ok=True)
        conf = vlib.run(
            self.configure_argv(build, prefix),
            cwd=self.repo,
            env=self.env(),
            timeout=CONFIGURE_TIMEOUT,
            log=self.log,
            label=f"altprefix-configure-{self.config}",
        )
        self.outcome.extra["altprefix-configure"] = conf
        if not conf.ok:
            return conf, prefix
        built = vlib.run(
            ["cmake", "--build", str(build), "--parallel"],
            cwd=self.repo,
            env=self.env(),
            timeout=self.PROBE_BUILD_TIMEOUT,
            log=self.log,
            label=f"altprefix-build-{self.config}",
        )
        self.outcome.extra["altprefix-build"] = built
        if not built.ok:
            return built, prefix
        inst = vlib.run(
            ["cmake", "--install", str(build)],
            cwd=self.repo,
            env=self.env(),
            timeout=INSTALL_TIMEOUT,
            log=self.log,
            label=f"altprefix-install-{self.config}",
        )
        self.outcome.extra["altprefix-install"] = inst
        self.alt_build = build
        self.alt_prefix = prefix
        return inst, prefix

    # What each SKIP_INSTALL_* switch must suppress once the product is a jar.
    # SKIP_INSTALL_HEADERS is deliberately absent: State B installs no headers, so
    # the switch has nothing left to suppress and asserting an empty tree twice
    # would grade the same fact under two names.
    SKIP_MODES: tuple[tuple[str, str, bool], ...] = (
        ("all", "SKIP_INSTALL_ALL", False),
        ("libraries", "SKIP_INSTALL_LIBRARIES", False),
        ("files", "SKIP_INSTALL_FILES", True),
    )

    def probe_skip_install(self, scratch: Path | None = None) -> dict[str, Path]:
        """SKIP_INSTALL_* must still suppress exactly what it names.

        Distributions use these to split zlib into runtime and development
        packages, so they are part of the build interface rather than a
        convenience, and they must survive a change of implementation language for
        the same reason the target names do: a downstream that sets them is not
        rebuilt when zlib is.

        Each mode gets a fresh build directory rather than reusing the
        alternate-prefix one.  The C form reused it and had to pass the four
        INSTALL_*_DIR variables explicitly to work around them being `CACHE PATH`
        values that stick at their first configure.  A fresh directory sidesteps
        that entirely, and for a build whose compile step is javac over a few
        thousand lines the extra configure costs less than the workaround did.

        Returns a mode -> prefix map; the third element of SKIP_MODES records
        whether the jar is expected to survive that mode, which is what the case
        grades against.
        """
        scratch = self.scratch if scratch is None else scratch
        prefixes: dict[str, Path] = {}
        for mode, option, jar_expected in self.SKIP_MODES:
            build = scratch / f"skipbuild-{self.config}-{mode}"
            prefix = scratch / f"skipprefix-{self.config}-{mode}"
            shutil.rmtree(build, ignore_errors=True)
            shutil.rmtree(prefix, ignore_errors=True)
            prefix.mkdir(parents=True, exist_ok=True)
            prefixes[mode] = prefix
            conf = vlib.run(
                self.configure_argv(build, prefix) + [f"-D{option}=ON"],
                cwd=self.repo,
                env=self.env(),
                timeout=CONFIGURE_TIMEOUT,
                log=self.log,
                label=f"skip-{mode}-configure-{self.config}",
            )
            self.outcome.extra[f"skip-{mode}-configure"] = conf
            if not conf.ok:
                continue
            built = vlib.run(
                ["cmake", "--build", str(build), "--parallel"],
                cwd=self.repo,
                env=self.env(),
                timeout=self.PROBE_BUILD_TIMEOUT,
                log=self.log,
                label=f"skip-{mode}-build-{self.config}",
            )
            self.outcome.extra[f"skip-{mode}-build"] = built
            if not built.ok:
                continue
            inst = vlib.run(
                ["cmake", "--install", str(build)],
                cwd=self.repo,
                env=self.env(),
                timeout=INSTALL_TIMEOUT,
                log=self.log,
                label=f"skip-{mode}-install-{self.config}",
            )
            self.outcome.extra[f"skip-{mode}-install"] = inst
            jar_present = (prefix / JAR_RELPATH).is_file()
            self.outcome.extra[f"skip-{mode}-jar"] = vlib.synthetic(
                f"skip-{mode}:jar",
                jar_present == jar_expected,
                f"{option}=ON: jar {'present' if jar_present else 'absent'}, "
                f"expected {'present' if jar_expected else 'absent'}",
            )
        return prefixes

    def probe_cache(self) -> Result:
        """Read the configured cache back out, for the version/option cases."""
        result = vlib.run(
            ["cmake", "-L", "-N", "-B", str(self.build_dir)],
            cwd=self.repo,
            env=self.env(),
            timeout=120.0,
            log=self.log,
            label=f"cache-{self.config}",
        )
        self.outcome.extra["cache"] = result
        return result

    def probe_ctest(self) -> Result:
        """Upstream registers `example` as a test; the registration must survive.

        `enable_testing()` and `add_test(example example)` are both unconditional
        in State A, so `ctest -N` lists the test whether or not it is run.  This
        lists rather than runs, because running it is already covered by the
        example driver's own differential case -- and because after the port
        `example` is a shell wrapper around `java`, so running it here would grade
        the JVM's startup rather than the registration.
        """
        result = vlib.run(
            ["ctest", "-N"],
            cwd=self.build_dir,
            env=self.env(),
            timeout=120.0,
            log=self.log,
            label=f"ctest-list-{self.config}",
        )
        self.outcome.extra["ctest"] = result
        return result


class ReferenceBuilder:
    """Builds the pinned C reference every behavioural case is graded against.

    This is the oracle.  It is built from the same tarball the agent received,
    with a real compiler, in this image -- so the expected value of every case is
    computed rather than transcribed, and the reference's own quirks (which bytes a
    given level emits, what `example` prints, how `gzprintf` rounds) are the
    specification by construction.

    Only the shared configuration is built, and that is a change from the C form
    of this task.  There, the reference was built twice because the C consumer was
    linked against one tree statically and the other dynamically, and because the
    versioned ELF ABI lived in the shared tree.  Here the reference exists solely
    to answer questions: nothing is linked against it except the verifier's own
    probe.c and consumer.c, and a jar has no static-link mode to contrast with.
    Building it once halves the slowest part of a run.

    A prefix that already exists is reused, because the reference is deterministic
    and rebuilding it per phase would cost the same time for the same answer.
    """

    def __init__(self, source: Path, workspace: Path, log: Log) -> None:
        self.source = source.resolve()
        self.workspace = workspace.resolve()
        self.log = log

    def build(self, config: str = "shared") -> Path:
        shared = "ON" if config == "shared" else "OFF"
        build_dir = self.workspace / f"ref-build-{config}"
        prefix = self.workspace / f"ref-install-{config}"
        if (prefix / "include" / "zlib.h").exists():
            self.log.write(f"reference ({config}) already built at {prefix}")
            return prefix
        self.log.section(f"build reference ({config})")
        # Deliberately *not* self.env(): the reference is C, and the shim exists to
        # stop C being compiled.  This is the one build in the run that gets a real
        # compiler on a clean PATH.
        vlib.run(
            [
                "cmake",
                "-S", str(self.source),
                "-B", str(build_dir),
                "-DCMAKE_BUILD_TYPE=Release",
                f"-DBUILD_SHARED_LIBS={shared}",
                "-DZLIB_BUILD_EXAMPLES=ON",
                f"-DCMAKE_INSTALL_PREFIX={prefix}",
            ],
            timeout=CONFIGURE_TIMEOUT,
            log=self.log,
            label=f"ref-configure-{config}",
            check=True,
        )
        vlib.run(
            ["cmake", "--build", str(build_dir), "--parallel", "4"],
            timeout=BUILD_TIMEOUT,
            log=self.log,
            label=f"ref-build-{config}",
            check=True,
        )
        vlib.run(
            ["cmake", "--install", str(build_dir)],
            timeout=INSTALL_TIMEOUT,
            log=self.log,
            label=f"ref-install-{config}",
            check=True,
        )
        return prefix

    def build_dir_for(self, config: str = "shared") -> Path:
        """Where a built reference's drivers live.

        The driver cases run `example` and `minigzip` out of the build tree, not
        the install tree -- upstream installs neither -- so the reference's build
        directory is part of what this class hands back.
        """
        return self.workspace / f"ref-build-{config}"


# -- the C half of the differential pair -------------------------------------


def _link_args(prefix: Path, *, static: bool) -> list[str]:
    """How a downstream C program links against the reference install tree."""
    libdir = prefix / "lib"
    if static:
        return [f"-L{libdir}", "-l:libz.a"]
    return [f"-L{libdir}", "-lz", f"-Wl,-rpath,{libdir}"]


def compile_probe(
    probe_src: Path,
    prefix: Path,
    output: Path,
    log: Log,
    *,
    label: str,
    static: bool = False,
) -> Result:
    """Compile the C half of the probe against the reference install tree.

    In the C-to-C form of this task this function compiled against the submission
    as well, and that was the point: a downstream C program that still builds is
    the acceptance test for a C-to-C migration.  Here it compiles against the
    reference only.  There is no C to link against in State B -- that is the
    migration -- so the corresponding question for the submission is asked by
    compile_java_probe() below, and the two together are the matched pair.

    Only the install tree's own include directory is added, so a probe that
    compiled because /usr/include/zlib.h was found instead would fail here rather
    than silently grading the system library.
    """
    argv = [
        "/usr/bin/cc",
        "-std=c11",
        "-O1",
        "-Wall",
        "-o", str(output),
        str(probe_src),
        f"-I{prefix / 'include'}",
    ] + _link_args(prefix, static=static)
    return vlib.run(
        argv, env=vlib.base_env(), timeout=300.0, log=log, label=label
    )


def compile_consumer_c(
    consumer_src: Path,
    prefix: Path,
    output: Path,
    log: Log,
    *,
    label: str,
    static: bool = False,
) -> Result:
    """Compile the C consumer used to produce the reference `cli` expectations.

    Same contract as compile_probe.  The pkg-config query the C form made is gone
    with the .pc file: State B installs none, deliberately, and pkg-config's answer
    about the *reference* tree would tell the verifier nothing about the
    submission.  The reference's own include and lib directories are named
    explicitly instead.
    """
    argv = [
        "/usr/bin/cc",
        "-std=c11",
        "-O1",
        "-Wall",
        "-o", str(output),
        str(consumer_src),
        f"-I{prefix / 'include'}",
    ] + _link_args(prefix, static=static)
    return vlib.run(
        argv, env=vlib.base_env(), timeout=300.0, log=log, label=label
    )


# -- the Java half of the differential pair -----------------------------------
#
# Two linkage modes are exercised throughout, and they are not interchangeable.
#
#   module    -- the jar goes on the module path and the module is resolved by
#                name.  This is the mode in which module-info.class is load
#                bearing: a descriptor that forgets to export org.zlib fails here
#                with an access error at compile time.
#   classpath -- the jar goes on the class path, where module-info.class is inert
#                and every public class is reachable regardless of what the
#                descriptor says.
#
# A submission that only ever ran one way usually cannot run the other, and the
# failures look nothing alike, which is why both are graded.  The verifier's own
# sources are in the unnamed package deliberately: a class in the unnamed package
# cannot belong to a named module, so the same file works in both modes and the
# mode is a property of the command line rather than of the source.

MODE_MODULE = "module"
MODE_CLASSPATH = "classpath"
JAVA_MODES = (MODE_MODULE, MODE_CLASSPATH)

# Properties pinned on every JVM the verifier starts.
#
# `java.library.path=` is the runtime half of "no system libz": the code policy
# forbids loading one, and with an empty library path System.loadLibrary("z") finds
# nothing to load.  The static half is the constant-pool scan; this is what makes
# the attempt fail at the point of the attempt.
#
# The locale and encoding properties are about the differential comparison rather
# than about cheating.  probe.c formats its records through printf under LC_ALL=C;
# Probe.java formats its through String.format, which consults the *default* locale
# unless told otherwise, and a default locale with non-ASCII digits or a comma
# decimal separator would make every numeric record differ from the C half for a
# reason that has nothing to do with the port.  Pinning them here means the pair
# compares the library rather than the environment.
JAVA_PROPERTIES = (
    "-Djava.library.path=",
    "-Duser.language=en",
    "-Duser.country=US",
    "-Dfile.encoding=UTF-8",
    "-Duser.timezone=UTC",
)


def java_compile(
    sources: list[Path],
    output_dir: Path,
    log: Log,
    *,
    label: str,
    mode: str = MODE_MODULE,
    jar: Path | None = None,
    module_name: str = MODULE_NAME,
    extra_args: tuple[str, ...] = (),
) -> Result:
    """Compile verifier-side Java against one install tree's jar.

    `--release 17` rather than `-source`/`-target`: `--release` compiles against
    the Java 17 API signatures, so verifier code that accidentally used a method
    added in 21 fails to compile here instead of failing to run on the graded JVM.

    No `-Werror`.  This is the verifier's own code, already lint-clean, and a
    warning arriving from the *submission's* jar -- a deprecated method in its
    public surface, say -- must not stop the probe building.  What a submission's
    own warnings cost it is graded elsewhere.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        java_tool("javac"),
        "--release", JAVA_RELEASE,
        "-d", str(output_dir),
    ]
    if jar is not None:
        if mode == MODE_MODULE:
            # --add-modules is required, not decorative: nothing in the unnamed
            # package `requires` org.zlib, so without it the module sits on the
            # path unresolved and every reference to it fails to compile.
            argv += ["-p", str(jar), "--add-modules", module_name]
        else:
            argv += ["-cp", str(jar)]
    argv += list(extra_args)
    argv += [str(src) for src in sources]
    return vlib.run(
        argv, env=vlib.base_env(), timeout=JAVAC_TIMEOUT, log=log, label=label
    )


def java_command(
    main: str,
    *,
    mode: str = MODE_MODULE,
    jar: Path | None = None,
    class_dir: Path | None = None,
    module_name: str = MODULE_NAME,
    properties: tuple[str, ...] = JAVA_PROPERTIES,
) -> list[str]:
    """The argv prefix that runs `main` against one install tree's jar.

    Returned as a list rather than executed because the behavioural executor holds
    this prefix for a whole run and appends per-case arguments to it a few thousand
    times.  Building it in one place is also what keeps the two modes honest: the
    same function answers both, so they cannot drift into being configured
    differently for reasons nobody recorded.
    """
    argv = [java_tool("java"), *properties]
    if mode == MODE_MODULE:
        if jar is not None:
            argv += ["-p", str(jar), "--add-modules", module_name]
        if class_dir is not None:
            argv += ["-cp", str(class_dir)]
    else:
        entries = [p for p in (jar, class_dir) if p is not None]
        if entries:
            argv += ["-cp", os.pathsep.join(str(p) for p in entries)]
    argv.append(main)
    return argv


def probe_class_dir(scratch: Path, config: str, mode: str) -> Path:
    """Where a compiled Probe.class lands, per configuration and linkage mode.

    Four directories rather than one.  The compiled probe is tied to the jar it was
    compiled against -- javac inlines a `static final int` at the use site, which
    is exactly the ABI property the constant cases exist to catch -- so a probe
    built against one configuration's jar must not be reused for the other's.
    """
    return scratch / f"probe-{config}-{mode}"


def compile_java_probe(
    probe_src: Path,
    jar: Path,
    scratch: Path,
    log: Log,
    *,
    config: str,
    mode: str = MODE_MODULE,
) -> tuple[Result, Path]:
    """Compile Probe.java against a submission's installed jar.

    The Java half of the acceptance test compile_probe() is the C half of, and the
    case the whole behavioural suite rests on: if a downstream Java program cannot
    compile against what the submission installed, no behavioral comparison is
    possible at all.  Saying so as its own case is what keeps that failure legible
    -- otherwise it arrives as several thousand unrelated case failures with no
    indication that the cause was one missing method.

    Only the installed jar is on the path.  Nothing from the build tree and no
    directory of loose classes: a probe that compiled because it found the
    submission's classes outside the artifact would be grading something the
    release does not ship.
    """
    out = probe_class_dir(scratch, config, mode)
    shutil.rmtree(out, ignore_errors=True)
    result = java_compile(
        [probe_src], out, log,
        label=f"probe-javac-{config}-{mode}", mode=mode, jar=jar,
    )
    return result, out


def jar_describe_module(jar: Path, log: Log, *, label: str) -> Result:
    """Ask the JDK what module it thinks the jar declares.

    A cross-check, not the source of truth.  The module cases are graded against
    classfile.py's own parse of module-info.class; this exists so a disagreement
    between that parse and the JDK's reading of the same bytes is reported rather
    than silently believed.
    """
    return vlib.run(
        [java_tool("jar"), "--describe-module", "--file", str(jar)],
        env=vlib.base_env(),
        timeout=60.0,
        log=log,
        label=label,
    )
