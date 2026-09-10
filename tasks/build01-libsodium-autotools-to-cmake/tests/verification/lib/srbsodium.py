"""What a stage-3 candidate is given to attack a migrated build system.

A candidate is a pytest file. It may import this module and the standard library,
and nothing else.

The difficulty this module exists to solve is specific to a build-toolchain
migration: the two trees do not have the same build system, so a candidate cannot
be handed "the published argv" the way it can when both sides are CMake. The
original is configured with `./configure`, the submission with `cmake`. If a
candidate had to know which it was looking at, it would be branching on the answer
instead of measuring it.

So the unit here is a *configuration*, named, and this module knows how each build
system spells it:

    default        the plain build                --   / -DCMAKE_BUILD_TYPE=Release
    minimal        --enable-minimal               /  -DSODIUM_MINIMAL=ON
    static-only    --disable-shared               /  -DSODIUM_BUILD_SHARED=OFF
    shared-only    --disable-static               /  -DSODIUM_BUILD_STATIC=OFF
    debug          --enable-debug                 /  -DCMAKE_BUILD_TYPE=Debug

    t = srbsodium.tree("default")     # configure + build + install, cached
    t.prefix                          # what it installed, and all you get to see

What a candidate sees is an install prefix. Not a build directory, not a build
log, not a source file, and nothing that names the build system that produced it.
A migration is graded on what it ships, and everything in scope for this stage is
visible in the install tree, in the ELF objects inside it, or in the behaviour of
the programs built from it.

The one exception is deliberate and narrow: `t.staged_install()`, `t.rebuild()`
and `t.build_tree_is_clean()` ask the build system to do something again, because
"installs correctly under DESTDIR" and "does not write into the source tree" are
properties of the build rather than of the install, both build systems express
them the same way, and neither is answerable from the prefix alone.

Building is lazy and cached per (tree, configuration). All five configurations
across both trees would be ten builds; a round usually needs two or three. The first
candidate to ask for one pays for it and every later candidate in every later
round gets it for free.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "CONFIGURATIONS", "TARGET_NAME", "SCRATCH", "SOVERSION", "SOFULL", "VERSION",
    "Output", "Tree", "TestRun", "BuildFailed", "tree", "CProgram",
    "ar_members", "nm_defined", "nm_undefined", "elf_dynamic",
    "elf_program_headers", "objdump_isa",
]

#: The version this migration is of. Not a candidate's to discover: State A's
#: install says 1.0.20 / libsodium.so.26.2.0, and a submission that changed it
#: fails a stage-2 check, not this one.
VERSION = "1.0.20"
SOVERSION = "26"
SOFULL = "26.2.0"

#: An opaque per-run label for the tree under test, for failure messages.  A label
#: naming the side -- "original" or "submission" -- makes a ten-point break out
#: of one line: `assert TARGET_NAME == "original"` passes on the original, fails on
#: the submission, and reproduces perfectly while establishing nothing about the
#: migration.  A hash says which tree without saying which side, so two failure
#: messages from one comparison are still distinguishable and branching on it buys
#: nothing.  Which side is which is decided by what the tree contains, which is why
#: configurations are named rather than spelled out in each build
#: system's own flags.
TARGET_NAME = os.environ.get("SRB_TARGET_TOKEN", "?")

#: Per-(candidate, tree) scratch. Emptied between the two halves of a comparison,
#: so a helper compiled against the original is not still sitting here when the
#: same candidate runs against the submission.
SCRATCH = Path(os.environ.get("SRB_SCRATCH", "/tmp/srb-candidate"))

#: Where builds and installs live. Shared across candidates and rounds, which is
#: what makes the cache worth having.
_STATE = Path(os.environ.get("SRB_STATE", "/tmp/srb-verification/state"))

#: The tree under test, already copied by run-candidate.sh. A candidate must not
#: read it -- what is graded is what the build ships -- so it is not exported.
_TARGET = Path(os.environ.get("SRB_TARGET", "/nonexistent"))

#: A listing of the tree exactly as delivered, written by run-candidate.sh before
#: any build ran. Tree.source_tree_dirty() is the difference from it.
_PRISTINE = Path(os.environ.get("SRB_PRISTINE_MANIFEST",
                                str(_STATE / "pristine.txt")))

_JOBS = str(max(1, min((os.cpu_count() or 2), 8)))


# --------------------------------------------------------------------------- #
# The configurations, in both dialects
# --------------------------------------------------------------------------- #
# Each entry is what this configuration means to `./configure` and what it means
# to `cmake`. They have to be genuine equivalents: a configuration that asked the
# two build systems for different things would produce a divergence this stage
# would then blame on the migration.
#
# Deliberately absent:
#
#   notests   -DSODIUM_BUILD_TESTS=OFF has no configure equivalent -- automake
#             builds the test programs during `make check`, so "do not build the
#             tests" is not a thing State A can be asked. Stage 2 measures it.
#
#   nossp     --disable-ssp turns off one probed flag; the CMake option for it is
#             the submission's to name, and this stage may not require a spelling
#             the instruction did not publish.

@dataclass(frozen=True)
class Configuration:
    name: str
    configure_args: tuple[str, ...]
    cmake_args: tuple[str, ...]
    about: str

CONFIGURATIONS = {
    c.name: c for c in [
        Configuration(
            "default", (), ("-DCMAKE_BUILD_TYPE=Release",),
            "the plain build: shared and static, tests on, optimised"),
        Configuration(
            "minimal", ("--enable-minimal",),
            ("-DCMAKE_BUILD_TYPE=Release", "-DSODIUM_MINIMAL=ON"),
            "the reduced feature set: SODIUM_LIBRARY_MINIMAL, a smaller symbol "
            "table, and nine fewer test programs"),
        Configuration(
            "static-only", ("--disable-shared",),
            ("-DCMAKE_BUILD_TYPE=Release", "-DSODIUM_BUILD_SHARED=OFF"),
            "libsodium.a and no shared object at all"),
        Configuration(
            "shared-only", ("--disable-static",),
            ("-DCMAKE_BUILD_TYPE=Release", "-DSODIUM_BUILD_STATIC=OFF"),
            "libsodium.so.26.2.0 and no archive"),
        # --enable-debug is `-O -g3 -DDEBUG=1 -U_FORTIFY_SOURCE`, and the -U does
        # not take effect: AX_ADD_FORTIFY_SOURCE runs 27 lines later in
        # configure.ac and appends -D_FORTIFY_SOURCE=3 after it, so State A's
        # debug build IS fortified. Verified on State A -- the debug .so imports
        # __memcpy_chk, __memset_chk and __explicit_bzero_chk exactly as the
        # release build does. Said plainly here because a port that read
        # configure.ac and implemented the intent rather than the behaviour
        # produces a real divergence, and that divergence is in scope.
        Configuration(
            "debug", ("--enable-debug",),
            ("-DCMAKE_BUILD_TYPE=Debug",),
            "the maintainer build: -O -g3, DEBUG=1, the extra warning flags, and "
            "fortification still on despite the -U that tries to turn it off"),
    ]
}


# --------------------------------------------------------------------------- #
# Running things
# --------------------------------------------------------------------------- #

@dataclass
class Output:
    """The result of running something. Streams are bytes, deliberately.

    A libsodium test program's output is compared byte for byte against a golden
    file, and several of the interesting cases are about exactly which bytes.
    Decoding here would hide the difference a candidate is looking for.
    """

    argv: list[str]
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def text(self, errors: str = "replace") -> str:
        return self.stdout.decode("utf-8", errors)

    def __str__(self) -> str:
        head = f"{' '.join(self.argv)} -> {self.returncode}"
        if self.timed_out:
            head += " (timed out)"
        tail = self.stderr.decode("utf-8", "replace").strip()
        return f"{head}\n{tail[:2000]}" if tail else head


def _clean_env(**extra: str) -> dict[str, str]:
    """The environment every subprocess here gets.

    Fixed, and fixed identically for both trees: a build that consulted CFLAGS
    from the ambient environment, or a program that consulted the locale, would
    otherwise answer differently on the two halves of a comparison for a reason
    that is not the migration.
    """
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LC_ALL": "C",
        "LANG": "C",
        "TZ": "UTC",
        "HOME": str(SCRATCH),
        # autogen.sh reaches for updated config.guess/config.sub otherwise, and
        # this container has no network.
        "DO_NOT_UPDATE_CONFIG_SCRIPTS": "1",
    }
    env.update(extra)
    return env


def _run(argv, *, stdin: bytes = b"", timeout: float = 60.0,
         cwd: Path | None = None, env: dict[str, str] | None = None) -> Output:
    argv = [str(a) for a in argv]
    try:
        proc = subprocess.run(
            argv, input=stdin, capture_output=True, timeout=timeout,
            cwd=str(cwd) if cwd else None, env=env or _clean_env(),
        )
    except subprocess.TimeoutExpired as exc:
        return Output(argv, 124, exc.stdout or b"", exc.stderr or b"",
                      timed_out=True)
    except FileNotFoundError as exc:
        return Output(argv, 127, b"", str(exc).encode())
    return Output(argv, proc.returncode, proc.stdout, proc.stderr)


# --------------------------------------------------------------------------- #
# A configured, built, installed tree
# --------------------------------------------------------------------------- #

class BuildFailed(AssertionError):
    """The tree under test would not build in this configuration.

    Raised rather than returned, because it is not a finding: a submission that
    does not build is stage 2's verdict, in an image built to measure it, and it is
    scored there out of 40.

    Letting it escape does not report a defect. lib/srbfault.py, registered by
    run-candidate.sh, turns an uncaught BuildFailed into exit 71 -- which the
    adjudicator reads as "this tree could not be tested" and answers `invalid` for,
    on whichever tree it happened to be. Requiring a PASS on the original is not
    what stops this from being a free finding, because a submission that will not
    build passes on the original: the round would see a divergence, six times, and
    report a defect that stage 2 owns.

    Assert on it if a configuration genuinely is your subject: catching it and
    asserting the message tells you which step failed, and a candidate that does
    that and passes has run -- srbfault does not fire for a BuildFailed you caught.
    """


@dataclass
class Tree:
    """One configuration of the tree under test, built and installed.

    Everything a candidate should touch hangs off ``prefix``. The properties
    below are conveniences over it, not extra access: ``libdir``, ``includedir``
    and the rest are paths inside the install.
    """

    config: Configuration
    root: Path                 # the copy of the tree that was built
    build_dir: Path
    prefix: Path
    log: Path
    _tests: object | None = field(default=None, repr=False)

    # -- the install tree --------------------------------------------------

    @property
    def libdir(self) -> Path:
        return self.prefix / "lib"

    @property
    def includedir(self) -> Path:
        return self.prefix / "include"

    @property
    def bindir(self) -> Path:
        return self.prefix / "bin"

    @property
    def pc_file(self) -> Path:
        return self.libdir / "pkgconfig" / "libsodium.pc"

    def shared_lib(self) -> Path:
        """The installed real shared object. Raises if there is not one.

        Looked up by SONAME-versioned name rather than by globbing, because
        "libsodium.so.26.2.0 is what got installed" is part of the contract and a
        glob would quietly accept libsodium.so.27.
        """
        path = self.libdir / f"libsodium.so.{SOFULL}"
        if not path.is_file():
            raise AssertionError(
                f"{path.name} is not installed in {self.libdir} on "
                f"{TARGET_NAME} ({self.config.name}); lib/ holds "
                f"{sorted(p.name for p in self.libdir.iterdir()) if self.libdir.is_dir() else 'nothing'}")
        return path

    def static_lib(self) -> Path:
        path = self.libdir / "libsodium.a"
        if not path.is_file():
            raise AssertionError(
                f"libsodium.a is not installed in {self.libdir} on "
                f"{TARGET_NAME} ({self.config.name})")
        return path

    def installed(self) -> list[str]:
        """Every installed path, prefix-relative, sorted.

        Symlinks are listed rather than followed, and a symlinked directory is
        listed instead of being descended into, because the symlink chain from
        libsodium.so to libsodium.so.26.2.0 is part of what a downstream linker
        consumes -- resolving it here would report the same file three times and
        lose the thing that matters.
        """
        if not self.prefix.is_dir():
            return []
        out = []
        for dirpath, dirnames, filenames in os.walk(self.prefix):
            here = Path(dirpath)
            for name in list(dirnames):
                if (here / name).is_symlink():
                    out.append(str((here / name).relative_to(self.prefix)))
                    dirnames.remove(name)
            for name in filenames:
                out.append(str((here / name).relative_to(self.prefix)))
        return sorted(out)

    def symlinks(self) -> dict[str, str]:
        """Installed symlinks, prefix-relative path -> raw target.

            assert t.symlinks()["lib/libsodium.so"] == "libsodium.so.26.2.0"

        Raw, not resolved: a link installed with an absolute target pointing into
        the build machine's prefix resolves correctly here and is broken for
        everyone else, and that difference is only visible before resolution.
        """
        out = {}
        for rel in self.installed():
            path = self.prefix / rel
            if path.is_symlink():
                out[rel] = os.readlink(path)
        return out

    def read(self, relpath: str) -> bytes:
        """Read an installed file. Raises with the directory listing if absent."""
        path = self.prefix / relpath
        if not path.is_file():
            here = path.parent
            listing = (sorted(p.name for p in here.iterdir())
                       if here.is_dir() else f"{here} does not exist")
            raise AssertionError(
                f"{relpath} is not installed on {TARGET_NAME} "
                f"({self.config.name}); {here}: {listing}")
        return path.read_bytes()

    def pkg_config(self, *args: str) -> list[str]:
        """Ask this install's own libsodium.pc something.

            assert t.pkg_config("--modversion") == ["1.0.20"]

        Returns [] if pkg-config failed, which for a missing or malformed .pc
        file is the answer rather than an error.
        """
        out = _run(["pkg-config", *args, "libsodium"], timeout=60.0,
                   env=_clean_env(PKG_CONFIG_PATH=str(self.libdir / "pkgconfig")))
        if not out.ok:
            return []
        return out.stdout.decode("utf-8", "replace").split()

    # -- asking the build to do something again ---------------------------

    def staged_install(self, timeout: float = 900.0) -> tuple[Output, Path]:
        """Install again into a DESTDIR, and return (output, the staging root).

        Both build systems spell this the same way -- `DESTDIR=... make install`,
        `DESTDIR=... cmake --install` -- and the property is the same in both: the
        staged tree under $DESTDIR$prefix must match a direct install, and nothing
        may be written outside $DESTDIR.
        """
        stage = self.build_dir.parent / f"destdir-{self.config.name}"
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        out = _installer_for(self.root).stage(self, stage, timeout=timeout)
        return out, stage

    def rebuild(self, timeout: float = 1800.0) -> Output:
        """Build again in the same build directory, without reconfiguring.

        An incremental no-op build. Both build systems are supposed to do nothing
        the second time; one that relinks the library, regenerates a header, or
        re-runs configure has a dependency stated wrongly, and a candidate can
        assert on the output.
        """
        return _installer_for(self.root).build(self, timeout=timeout)

    def source_tree_dirty(self) -> list[str]:
        """Paths any build in this stage has written into the source tree.

        Out-of-source means the source tree is untouched after a build. Compared
        against a manifest of the tree exactly as it was delivered, taken once
        before anything was configured, so a file the agent shipped is not
        mistaken for one the build produced.

        The answer is cumulative across configurations rather than per
        configuration: the trees share one source root, as they would for a user
        who builds twice, and attributing a stray file to whichever build ran
        first would make the answer depend on the order candidates happened to
        ask for things.

        Note for candidates: State A does NOT pass this. `autogen.sh` generates
        `configure`, `Makefile.in`, `aclocal.m4` and more in the source tree by
        design, so the original always reports a long list. A candidate asserting
        an empty list fails on the original and is worth nothing. What is worth
        something is a submission that writes something the original does not --
        a generated header landing in `src/`, an object file beside its source, a
        cache in the tree.
        """
        if not _PRISTINE.is_file():
            return []
        was = set(_PRISTINE.read_text().splitlines())
        return sorted(set(_manifest(self.root)) - was)

    def source_package(self, timeout: float = 1200.0) -> tuple[Output, list[str]]:
        """Ask the build for a source archive, and return (output, its contents).

        `make dist` and `cpack --config CPackSourceConfig.cmake` are the same
        request in the two dialects: package this repository for release. What
        comes out is comparable across them -- a tar of paths -- and what is in
        scope is the content: the library sources, the public headers and the test
        suite in, build products and version-control data out.

        Contents are archive-relative and stripped of the leading
        `libsodium-1.0.20/` component, because the top-level directory name is
        the archiver's choice and not the migration's subject.
        """
        out, entries = _installer_for(self.root).dist(self, timeout=timeout)
        return out, entries

    # -- the test suite ----------------------------------------------------

    def run_tests(self, timeout: float = 3600.0) -> "TestRun":
        """Run the installed configuration's own test suite.

        libsodium ships 80 test programs; each writes its output and compares it
        against a golden file, so "the suite is green" is a statement about the
        library's behaviour rather than about the build. Both build systems can be
        asked to run it (`make check`, `ctest`), and this returns the same shape
        either way: names, and which of them passed.

        Cached: the suite takes minutes, and a round that asks twice gets the same
        answer.
        """
        if self._tests is None:
            self._tests = _installer_for(self.root).run_tests(self, timeout=timeout)
        return self._tests  # type: ignore[return-value]


@dataclass
class TestRun:
    """The outcome of a test suite run, in build-system-neutral terms."""

    passed: tuple[str, ...]
    failed: tuple[str, ...]
    output: Output

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.passed + self.failed))

    @property
    def all_passed(self) -> bool:
        return bool(self.passed) and not self.failed

    def __str__(self) -> str:
        return (f"{len(self.passed)} passed, {len(self.failed)} failed"
                + (f"; failures: {', '.join(sorted(self.failed))}"
                   if self.failed else ""))


# --------------------------------------------------------------------------- #
# The two build systems
# --------------------------------------------------------------------------- #
# This is the only place in the stage that knows there are two, and it is not
# reachable from a candidate. Which one is used is decided by what the tree
# contains, not by which tree it is: a submission that kept a configure.ac would
# otherwise be built by the branch it was trying to escape, and a candidate would
# then measure State A twice and find nothing.

class _Autotools:
    """State A's build system: autogen.sh, configure, make, make install."""

    name = "autotools"

    @staticmethod
    def detect(root: Path) -> bool:
        return (root / "configure.ac").is_file() or (root / "configure").is_file()

    def configure(self, root: Path, build: Path, prefix: Path,
                  cfg: Configuration, log: Path, timeout: float) -> Output:
        # autogen.sh generates `configure` in the source tree. -s skips the
        # config script refresh, which needs a network this container does not
        # have.
        if not (root / "configure").is_file():
            out = _logged(log, ["./autogen.sh", "-s"], cwd=root, timeout=timeout)
            if not out.ok:
                return out
        build.mkdir(parents=True, exist_ok=True)
        return _logged(log, [str(root / "configure"),
                             f"--prefix={prefix}", *cfg.configure_args],
                       cwd=build, timeout=timeout)

    def build(self, t: Tree, timeout: float) -> Output:
        return _logged(t.log, ["make", f"-j{_JOBS}"], cwd=t.build_dir,
                       timeout=timeout)

    def install(self, t: Tree, timeout: float) -> Output:
        return _logged(t.log, ["make", "install"], cwd=t.build_dir,
                       timeout=timeout)

    def stage(self, t: Tree, destdir: Path, timeout: float) -> Output:
        return _logged(t.log, ["make", "install", f"DESTDIR={destdir}"],
                       cwd=t.build_dir, timeout=timeout)

    def dist(self, t: Tree, timeout: float) -> tuple[Output, list[str]]:
        out = _logged(t.log, ["make", "dist"], cwd=t.build_dir, timeout=timeout)
        return out, _archive_entries(t.build_dir)

    def run_tests(self, t: Tree, timeout: float) -> TestRun:
        # `make check` writes one .trs per test program with a TAP-ish result
        # line. Parsing those rather than make's stdout because a parallel run
        # interleaves the stdout of eighty programs.
        out = _logged(t.log, ["make", "check", f"-j{_JOBS}"], cwd=t.build_dir,
                      timeout=timeout)
        passed, failed = [], []
        for trs in sorted((t.build_dir / "test" / "default").glob("*.trs")):
            name = trs.stem
            text = trs.read_text(errors="replace")
            if re.search(r"^:test-result:\s*PASS", text, re.M):
                passed.append(name)
            else:
                failed.append(name)
        if not passed and not failed:
            # No .trs files at all: the suite did not run. Report the whole run
            # as one failure named for what was asked, rather than as "0 tests
            # passed", which a candidate could mistake for an empty suite.
            failed.append("make-check")
        return TestRun(tuple(passed), tuple(failed), out)


class _CMake:
    """What the submission is supposed to be: cmake, cmake --build, cmake --install."""

    name = "cmake"

    @staticmethod
    def detect(root: Path) -> bool:
        return (root / "CMakeLists.txt").is_file()

    def configure(self, root: Path, build: Path, prefix: Path,
                  cfg: Configuration, log: Path, timeout: float) -> Output:
        return _logged(log, ["cmake", "-S", str(root), "-B", str(build),
                             f"-DCMAKE_INSTALL_PREFIX={prefix}",
                             *cfg.cmake_args],
                       cwd=root, timeout=timeout)

    def build(self, t: Tree, timeout: float) -> Output:
        return _logged(t.log, ["cmake", "--build", str(t.build_dir),
                               "--parallel", _JOBS], cwd=t.root, timeout=timeout)

    def install(self, t: Tree, timeout: float) -> Output:
        return _logged(t.log, ["cmake", "--install", str(t.build_dir)],
                       cwd=t.root, timeout=timeout)

    def stage(self, t: Tree, destdir: Path, timeout: float) -> Output:
        return _logged(t.log, ["cmake", "--install", str(t.build_dir)],
                       cwd=t.root, timeout=timeout,
                       env=_clean_env(DESTDIR=str(destdir)))

    def dist(self, t: Tree, timeout: float) -> tuple[Output, list[str]]:
        # The two documented spellings. `package_source` is a target and needs
        # the build directory; cpack needs the config CMake generated in it. Try
        # the target first: a submission that customised CPack has done so
        # through it.
        out = _logged(t.log, ["cmake", "--build", str(t.build_dir),
                              "--target", "package_source"],
                      cwd=t.root, timeout=timeout)
        if not out.ok:
            out = _logged(t.log, ["cpack", "--config",
                                  str(t.build_dir / "CPackSourceConfig.cmake")],
                          cwd=t.build_dir, timeout=timeout)
        return out, _archive_entries(t.build_dir)

    def run_tests(self, t: Tree, timeout: float) -> TestRun:
        out = _logged(t.log, ["ctest", "--output-on-failure", "-j", _JOBS],
                      cwd=t.build_dir, timeout=timeout)
        passed, failed = [], []
        # ctest's per-test lines: "  1/80 Test  #1: auth ......  Passed  0.01 sec"
        for line in out.stdout.decode("utf-8", "replace").splitlines():
            m = re.match(r"\s*\d+/\d+\s+Test\s+#\d+:\s+(\S+)\s+\.+\s*(\S+)", line)
            if not m:
                continue
            name, verdict = m.group(1), m.group(2)
            (passed if verdict == "Passed" else failed).append(name)
        if not passed and not failed:
            failed.append("ctest")
        return TestRun(tuple(passed), tuple(failed), out)


_INSTALLERS = (_CMake, _Autotools)


def _installer_for(root: Path):
    """Pick the build system this tree actually has.

    CMake first: a submission that migrated correctly has only a CMakeLists.txt,
    and a submission that kept both is built as CMake -- which is the reading that
    matches what stage 1 judged and what the instruction asked for. A tree with
    neither is a build failure named as one, not a traceback.
    """
    for cls in _INSTALLERS:
        if cls.detect(root):
            return cls()
    raise BuildFailed(
        f"{TARGET_NAME} has no build system this stage can drive: no "
        f"CMakeLists.txt and no configure.ac at {root}")


def _logged(log: Path, argv, *, cwd: Path, timeout: float,
            env: dict[str, str] | None = None) -> Output:
    """Run a build step, appending everything to the log.

    The log is on disk rather than in the returned Output's place in a report
    because a failing libsodium build produces megabytes, and a candidate needs
    the tail of it in an assertion message, not all of it.
    """
    out = _run(argv, cwd=cwd, timeout=timeout, env=env)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as fh:
        fh.write(f"\n$ {' '.join(str(a) for a in argv)}\n".encode())
        fh.write(out.stdout)
        fh.write(out.stderr)
        fh.write(f"[exit {out.returncode}]\n".encode())
    return out


def _archive_entries(where: Path) -> list[str]:
    """Contents of the newest source archive under `where`, one entry per path.

    Both build systems drop the archive in the build directory and neither is
    obliged to name it the same way, so this finds the most recently written
    tar/zip rather than a fixed name -- the archive's own name is the packager's
    choice, and requiring a spelling the instruction did not publish would put a
    candidate out of scope.
    """
    candidates = [p for p in where.iterdir()
                  if p.is_file() and p.name.endswith(
                      (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".zip"))]
    if not candidates:
        return []
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    if newest.suffix == ".zip":
        out = _run(["unzip", "-Z1", str(newest)], timeout=300.0)
    else:
        out = _run(["tar", "tf", str(newest)], timeout=300.0)
    entries = []
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        # Strip the leading directory component: `libsodium-1.0.20/src/...` and
        # `libsodium-1.0.20-Source/src/...` are the same package differently
        # named, and the name is not the subject.
        parts = line.split("/", 1)
        entries.append(parts[1] if len(parts) == 2 and parts[1] else line)
    return sorted(e for e in entries if e and not e.endswith("/"))


def _manifest(root: Path) -> list[str]:
    """Every path in a tree, relative and sorted. For the dirty-tree check."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            out.append(str((Path(dirpath) / name).relative_to(root)))
    return sorted(out)


# --------------------------------------------------------------------------- #
# tree(): configure, build, install, once
# --------------------------------------------------------------------------- #

_CACHE: dict[str, Tree] = {}


def tree(config: str = "default", *, timeout: float = 2700.0) -> Tree:
    """The tree under test, built and installed in this configuration.

        t = srbsodium.tree("minimal")
        assert b"sodium_library_minimal" in t.read("include/sodium/core.h")

    The first call for a configuration configures, builds and installs it, which
    takes minutes. Every later call in this round and in every other round returns
    the same install: the tree does not change between candidates, so rebuilding
    per candidate would spend the stage's budget on compilation instead of on
    finding defects.

    Raises BuildFailed if the tree will not build in this configuration. That is
    not a finding -- see BuildFailed -- but it is assertable if the configuration
    itself is your subject.
    """
    if config not in CONFIGURATIONS:
        raise AssertionError(
            f"unknown configuration {config!r}; this stage builds "
            f"{', '.join(sorted(CONFIGURATIONS))}. Each is a configuration BOTH "
            f"build systems can be asked for, which is what makes a comparison "
            f"between them mean anything.")
    if config in _CACHE:
        return _CACHE[config]

    cfg = CONFIGURATIONS[config]
    state = _STATE / TARGET_NAME / config
    build_dir = state / "build"
    prefix = state / "install"
    log = state / "build.log"
    marker = state / ".installed"

    state.mkdir(parents=True, exist_ok=True)

    # Candidates within a round run one at a time, but a round's own reruns and a
    # rerun of an upheld candidate can overlap. The lock makes the first arrival
    # build and the rest wait, rather than two processes writing one build tree.
    with _lock(state / ".lock"):
        if not marker.is_file():
            _build_once(cfg, state, build_dir, prefix, log, timeout)
            marker.write_text(f"{cfg.name}\n")

    t = Tree(cfg, _TARGET, build_dir, prefix, log)
    _CACHE[config] = t
    return t


def _build_once(cfg: Configuration, state: Path, build_dir: Path, prefix: Path,
                log: Path, timeout: float) -> None:
    installer = _installer_for(_TARGET)

    # The pristine manifest is run-candidate.sh's job, written before any build in
    # this stage ran. Written here too if it is somehow absent, which loses the
    # first configuration's contribution to source_tree_dirty() rather than
    # producing a wrong answer for every one of them.
    if not _PRISTINE.is_file():
        _PRISTINE.parent.mkdir(parents=True, exist_ok=True)
        _PRISTINE.write_text("\n".join(_manifest(_TARGET)) + "\n")

    shutil.rmtree(build_dir, ignore_errors=True)
    shutil.rmtree(prefix, ignore_errors=True)
    log.unlink(missing_ok=True)

    out = installer.configure(_TARGET, build_dir, prefix, cfg, log, timeout)
    if not out.ok:
        raise BuildFailed(_why("configure", cfg, log, out))

    t = Tree(cfg, _TARGET, build_dir, prefix, log)
    out = installer.build(t, timeout)
    if not out.ok:
        raise BuildFailed(_why("build", cfg, log, out))

    out = installer.install(t, timeout)
    if not out.ok:
        raise BuildFailed(_why("install", cfg, log, out))


def _why(step: str, cfg: Configuration, log: Path, out: Output) -> str:
    tail = ""
    if log.is_file():
        tail = log.read_text(errors="replace")[-3000:]
    return (f"{TARGET_NAME} failed to {step} in the {cfg.name} configuration "
            f"({cfg.about}).\nexit {out.returncode}"
            + (" (timed out)" if out.timed_out else "")
            + f"\n--- last of {log} ---\n{tail}")


class _lock:
    """A file lock, so two processes do not build one tree at once."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = self.path.open("w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()
        return False


# --------------------------------------------------------------------------- #
# Building a consumer against an install
# --------------------------------------------------------------------------- #

_CC = shutil.which("cc") or "/usr/bin/cc"


class CProgram:
    """A C program compiled against an installed libsodium, and run.

        prog = srbsodium.CProgram(t, '''
            #include <sodium.h>
            #include <stdio.h>
            int main(void) {
                if (sodium_init() < 0) return 1;
                printf("%s\\n", sodium_version_string());
                return 0;
            }
        ''')
        out = prog.run()
        assert out.stdout == b"1.0.20\\n"

    This is the main way to reach a build migration's actual subject. What a
    build system ships is a library other programs link against, and almost
    everything that can go wrong with a port shows up here: a missing symbol, a
    header that does not compile standalone, a .pc file with the wrong
    Libs.private, an archive that needs a library the .pc does not name, a
    visibility setting that hid something public.

    Compile flags come from the install's own libsodium.pc, via pkg-config,
    because that is how a downstream package finds them. A submission whose .pc
    file is wrong therefore fails to compile a candidate here -- which is a real
    defect rather than an artefact of this helper guessing paths. Pass
    `use_pkg_config=False` to fall back to -I/-L, which is how you tell the two
    apart.

    `static=True` links the archive instead of the shared object. Worth doing at
    least once: they are separate outputs of the build and a port can get one
    right and the other wrong.
    """

    def __init__(self, t: Tree, source: str, *, name: str = "prog",
                 static: bool = False, use_pkg_config: bool = True,
                 extra_cflags: tuple[str, ...] = (),
                 extra_ldflags: tuple[str, ...] = ()) -> None:
        self.tree = t
        self.source = textwrap.dedent(source)
        self.static = static
        self.use_pkg_config = use_pkg_config
        digest = hashlib.sha256(self.source.encode()).hexdigest()[:8]
        self.dir = SCRATCH / f"{name}-{t.config.name}-{digest}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.src_path = self.dir / f"{name}.c"
        self.bin_path = self.dir / name
        self.src_path.write_text(self.source, encoding="utf-8")
        self.compile_output: Output | None = None
        self._extra_cflags = tuple(extra_cflags)
        self._extra_ldflags = tuple(extra_ldflags)

    def _flags(self) -> tuple[list[str], list[str]]:
        cflags = self.tree.pkg_config("--cflags") if self.use_pkg_config else []
        if not cflags:
            cflags = [f"-I{self.tree.includedir}"]

        if self.static:
            # The archive named by path rather than through -lsodium, so a shared
            # object in the same directory cannot satisfy the link and make a
            # static configuration look like it worked. Libs.private follows it,
            # which is the point of asking --static: an archive needs whatever
            # libsodium itself needs, and a .pc file that omits those fails to
            # link right here.
            ldflags = [str(self.tree.libdir / "libsodium.a")]
            if self.use_pkg_config:
                ldflags += [f for f in self.tree.pkg_config("--libs", "--static")
                            if f != "-lsodium" and not f.startswith("-L")]
        elif self.use_pkg_config and self.tree.pkg_config("--libs"):
            ldflags = self.tree.pkg_config("--libs")
        else:
            ldflags = [f"-L{self.tree.libdir}", "-lsodium"]

        return (cflags + list(self._extra_cflags),
                ldflags + list(self._extra_ldflags))

    def compile(self, timeout: float = 180.0) -> Output:
        """Compile, returning the compiler's own Output rather than raising.

        Not an exception, because "a program that compiles against State A's
        install does not compile against this one" is itself a finding about the
        headers or the package config, and a candidate should be able to assert it
        directly.
        """
        cflags, ldflags = self._flags()
        argv = [_CC, "-std=c99", "-O1", "-o", str(self.bin_path),
                str(self.src_path), *cflags, *ldflags]
        self.compile_output = _run(argv, timeout=timeout, cwd=self.dir)
        return self.compile_output

    def build(self, timeout: float = 180.0) -> "CProgram":
        out = self.compile(timeout=timeout)
        if not out.ok:
            raise AssertionError(
                f"the helper did not compile against {TARGET_NAME} "
                f"({self.tree.config.name}, "
                f"{'static' if self.static else 'shared'}): {out}")
        return self

    def run(self, *args: str, stdin: bytes | str = b"",
            timeout: float = 120.0) -> Output:
        if self.compile_output is None:
            self.build()
        if not self.bin_path.exists():
            raise AssertionError(f"{self.bin_path} was never built")
        if isinstance(stdin, str):
            stdin = stdin.encode("utf-8")
        return _run([str(self.bin_path), *args], stdin=stdin, timeout=timeout,
                    cwd=self.dir,
                    env=_clean_env(LD_LIBRARY_PATH=str(self.tree.libdir)))


# --------------------------------------------------------------------------- #
# Reading what got installed
# --------------------------------------------------------------------------- #

def ar_members(path: Path, *, into: Path | None = None) -> dict[str, Path]:
    """Extract an archive's members and return name -> extracted path.

        members = srbsodium.ar_members(t.static_lib())
        assert "avx2" not in srbsodium.objdump_isa(members["poly1305_donna.o"])

    This is how the per-file ISA partition is visible from outside the build. The
    shared object is one linked image containing every implementation, so an AVX2
    instruction found in it says nothing; the archive still has one member per
    translation unit, and a baseline member that contains an AVX2 instruction was
    compiled with a flag it should not have had.

    Member names come from the archive, so they are whatever the build called its
    objects -- which is the submission's choice and not part of the contract. Match
    on the stem rather than on the full name.
    """
    dest = into or (SCRATCH / f"ar-{path.stem}")
    dest.mkdir(parents=True, exist_ok=True)
    listing = _run(["ar", "t", str(path)], timeout=120.0)
    if not listing.ok:
        raise AssertionError(f"{path} is not readable as an archive: {listing}")
    names = [n for n in listing.stdout.decode("utf-8", "replace").split()
             if n.endswith((".o", ".obj", ".lo"))]
    out = _run(["ar", "x", str(path), *names], cwd=dest, timeout=300.0)
    if not out.ok:
        raise AssertionError(f"could not extract {path}: {out}")
    return {n: dest / Path(n).name for n in names}


def nm_defined(path: Path) -> set[str]:
    """The defined, exported symbols of a library.

    `nm --defined-only --extern-only`. What libsodium's public ABI is, is the
    question this answers, and it is in scope in both directions here: a symbol
    sodium.h declares that is missing is a defect, and a symbol that is exported
    but should have been hidden by -fvisibility=hidden is also a defect, because
    visibility decides the ABI.
    """
    out = _run(["nm", "--defined-only", "--extern-only", "--format=posix",
                str(path)], timeout=120.0)
    if not out.ok:
        return set()
    names: set[str] = set()
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] in "TDBRWiu":
            names.add(parts[0])
    return names


def nm_undefined(path: Path) -> set[str]:
    """The symbols a library imports rather than defines.

    `nm --undefined-only`. This is the other half of the symbol question and the
    half that shows what the compile flags did, because several of the flags
    configure.ac probes are visible nowhere else:

      * _FORTIFY_SOURCE replaces a call to memcpy with a call to __memcpy_chk, so
        the fortified build imports the _chk family and the unfortified one does
        not. Nothing about the library's own exports changes, which is why
        nm_defined cannot see it.
      * -fstack-protector introduces __stack_chk_fail.
      * pthread support, and whether it was really found, shows up as
        pthread_create and friends being imported at all.

    Works on the archive as well as on the shared object, and on a single archive
    member -- which is how you ask whether one translation unit was fortified.
    """
    out = _run(["nm", "--undefined-only", "--format=posix", str(path)],
               timeout=120.0)
    if not out.ok:
        return set()
    names: set[str] = set()
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        # posix format: "name U" for undefined, "name w" for a weak reference.
        if len(parts) >= 2 and parts[1] in "Uvw":
            names.add(parts[0].split("@", 1)[0])
    return names


def elf_dynamic(path: Path) -> dict[str, list[str]]:
    """The dynamic section of an ELF file, tag -> values.

        d = srbsodium.elf_dynamic(t.shared_lib())
        assert d["SONAME"] == ["libsodium.so.26"]

    SONAME, NEEDED, RPATH, RUNPATH, FLAGS and FLAGS_1 are the interesting ones:
    the first is the ABI's name, the last two carry BIND_NOW and PIE, and an
    RPATH pointing at a build directory is a shipped artefact that only works on
    the machine that built it.
    """
    out = _run(["readelf", "-d", str(path)], timeout=120.0)
    tags: dict[str, list[str]] = {}
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        m = re.match(r"\s*0x[0-9a-f]+\s+\(([A-Z0-9_]+)\)\s+(.*)", line)
        if not m:
            continue
        tag, value = m.group(1), m.group(2).strip()
        v = re.sub(r"^(Shared library|Library soname|Library runpath|"
                   r"Library rpath):\s*", "", value)
        tags.setdefault(tag, []).append(v.strip("[]"))
    return tags


def elf_program_headers(path: Path) -> list[tuple[str, str]]:
    """(type, flags) for each program header. GNU_RELRO and GNU_STACK live here.

        headers = srbsodium.elf_program_headers(t.shared_lib())
        assert any(kind == "GNU_RELRO" for kind, _ in headers)
        assert ("GNU_STACK", "RW") in [(k, f.strip()) for k, f in headers]
    """
    out = _run(["readelf", "-lW", str(path)], timeout=120.0)
    headers: list[tuple[str, str]] = []
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        m = re.match(r"\s{2}([A-Z_]+)\s+0x[0-9a-f]+.*\s([RWE ]{3})\s+0x", line)
        if m:
            headers.append((m.group(1), m.group(2)))
    return headers


#: Mnemonic families, in the order the library's own dispatch names them. `v?`
#: throughout because a file compiled with -mavx gets the VEX encoding of the same
#: instruction -- vaesenc for aesenc -- and it is the same requirement on the CPU
#: either way.
_ISA_FAMILIES = (
    ("avx512", r"\bv[a-z0-9]+[^\n]*%[zk][0-9m]|\bvpternlog|\bkmov"),
    # AVX1 has no 256-bit integer arithmetic; AVX2 is what makes vpxor %ymm legal.
    ("avx2", r"\bvp(add|sub|and|or|xor|cmpeq|shuf|perm|srl|sll|mull?)[a-z0-9]*"
             r"[^\n]*%ymm"),
    ("avx", r"\bv[a-z0-9]+[^\n]*%ymm"),
    ("aesni", r"\bv?aes(enc|dec|keygenassist|imc)"),
    ("pclmul", r"\bv?pclmul"),
    ("sse41", r"\bv?(pblendw|pmovzxbd|pextrd|ptest|roundp)"),
    ("ssse3", r"\bv?(pshufb|palignr|phadd|pmaddubsw)"),
    ("sse2", r"\b(movdqa|paddq|pxor|punpck)"),
    ("rdrand", r"\brdrand"),
)

#: Any VEX-encoded SIMD instruction. Present in an object means -mavx or better,
#: whatever register width it happens to use.
_VEX_ENCODED = r"\bv(movdq[au]|p(xor|add|and|or|shuf)|aes|pclmul)"


def objdump_isa(path: Path) -> set[str]:
    """Instruction-set families an object file actually contains.

    Disassembles and classifies by mnemonic. This is the outside view of the
    per-file ISA partition: a baseline object holding an AVX2 instruction was
    compiled with a flag it should not have had, and it faults with SIGILL on
    hardware without the extension.

    Use it on archive members (see ar_members), not on the shared object: the .so
    is one linked image containing every alternative implementation, so finding
    AVX-512 in it proves only that the library has an AVX-512 code path, which it
    is supposed to.

    The classification is by mnemonic and register width, so it is conservative
    rather than exhaustive: it answers "does this object contain instructions from
    family X" and does not enumerate every instruction in an extension.

    Two things about reading the answer, both of which State A demonstrates and
    neither of which is obvious:

    * "sse2" is not evidence of anything. SSE2 is part of the x86-64 baseline ABI,
      so gcc emits movdqa and pxor into ordinary objects with no flags at all, and
      almost every member of this archive reports it. A candidate asserting that
      baseline objects contain no SIMD fails on the original.
    * When a file is compiled with -mavx alongside -maes, gcc emits the VEX forms
      -- vaesenc rather than aesenc, vpclmullqlqdq rather than pclmulqdq -- so the
      families are matched with the prefix optional. Their presence still means the
      object requires AES-NI; it additionally means it requires AVX.
    """
    out = _run(["objdump", "-d", "--no-show-raw-insn", str(path)], timeout=300.0)
    text = out.stdout.decode("utf-8", "replace")
    found = {family for family, pattern in _ISA_FAMILIES if re.search(pattern, text)}
    # A VEX-encoded instruction requires AVX even when no 256-bit register appears
    # anywhere in the object. That is the aesni case -- `-mavx -maes` gives vaesenc
    # on xmm -- and reporting AES-NI without the AVX it also needs would understate
    # what the object demands of the CPU it runs on.
    if re.search(_VEX_ENCODED, text):
        found.add("avx")
    return found
