#!/usr/bin/env python3
"""Builds the submission, and builds the pinned C reference to compare against.

The submission is built the way a distributor would: out-of-source CMake
configure, build, install to a prefix, in both link modes.  The argv used for
the graded build is the argv published in source-contract.json, verbatim --
including the absence of a `-G` flag, so the default generator is what gets
exercised.  Nothing about the build is special-cased for grading, with one
exception that only observes: the C and C++ compilers on PATH are replaced by a
shim that records which of its invocations compiled a repository source.  If the
migration really happened there is nothing to record; if C is still being
compiled the ledger says so and `compiler-shim-clean` fails on it.

The shim records rather than refuses because this stage grades behaviour against
expectations recorded from State A, and State A is the C implementation.  See
shim/ccshim.py for the modes and for why the graded path is the recording one.

The reference is built from the same pinned tarball the agent started from,
inside this image, with a real compiler.  Expected values are therefore produced
here rather than transcribed, which is what lets the comparison be byte-exact
without freezing a snapshot that could drift.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import vlib
from vlib import Log, Result

CONFIGURE_TIMEOUT = 900.0
BUILD_TIMEOUT = 2700.0
INSTALL_TIMEOUT = 600.0

# Every driver name a C or C++ compile could plausibly arrive through.  The
# assembler, archiver and linker are deliberately absent: rustc needs them.
SHIM_TOOLS = (
    "cc", "gcc", "gcc-12", "c++", "g++", "g++-12", "clang", "clang++", "cpp",
    "x86_64-linux-gnu-gcc", "x86_64-linux-gnu-gcc-12", "x86_64-linux-gnu-g++",
    "x86_64-linux-gnu-g++-12",
)

REAL_PATH = "/usr/local/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

#: The two shim modes, spelled here so a caller does not pass a string the shim
#: will reject.  `record` is the graded path; see shim/ccshim.py.
SHIM_RECORD = "record"
SHIM_ENFORCE = "enforce"


@dataclass
class BuildOutcome:
    """What happened in one link configuration."""

    config: str
    build_dir: Path
    prefix: Path
    configure: Result | None = None
    compile: Result | None = None
    install: Result | None = None
    extra: dict[str, Result] = field(default_factory=dict)
    shim_log: Path | None = None

    @property
    def configured(self) -> bool:
        return self.configure is not None and self.configure.ok

    @property
    def built(self) -> bool:
        return self.compile is not None and self.compile.ok

    @property
    def installed(self) -> bool:
        return self.install is not None and self.install.ok

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
    # The build runs in its own module, and the structural checks that read its
    # logs run in another.  summary() is the report and clips its output; these
    # two are the handover and do not, because check_build_warnings() searches
    # the build log for "undefined reference" and a needle elided from the middle
    # of a clipped log would read as a clean build.

    def persist(self, state_dir: Path) -> dict:
        """Write every captured stream to disk and return a path-only record."""
        logs = state_dir / f"logs-{self.config}"
        logs.mkdir(parents=True, exist_ok=True)
        record: dict = {
            "config": self.config,
            "build_dir": str(self.build_dir),
            "prefix": str(self.prefix),
            "shim_log": str(self.shim_log) if self.shim_log else "",
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


def shim_env(
    shim_dir: Path, shim_log: Path, repo: Path, *, mode: str = SHIM_RECORD,
    **overrides,
) -> dict:
    """An environment in which every repository C compile is recorded.

    `mode` is passed to the shim verbatim and decides only whether a repository
    compile is also refused; either way it lands in the ledger at `shim_log`.
    """
    env = vlib.base_env(
        PATH=f"{shim_dir}:{REAL_PATH}",
        CCSHIM_PATH=REAL_PATH,
        CCSHIM_LOG=str(shim_log),
        CCSHIM_REPO=str(repo),
        CCSHIM_MODE=mode,
        CMAKE_BUILD_PARALLEL_LEVEL="4",
    )
    env.update({k: v for k, v in overrides.items() if v is not None})
    return env


class Builder:
    """Drives one submission build in one link mode."""

    def __init__(
        self, repo: Path, workspace: Path, shim_dir: Path, log: Log, *, config: str,
        shim_mode: str = SHIM_RECORD,
    ) -> None:
        self.repo = repo
        self.config = config
        self.log = log
        self.build_dir = workspace / f"build-{config}"
        self.prefix = workspace / f"install-{config}"
        self.shim_dir = shim_dir
        self.shim_mode = shim_mode
        self.shim_log = workspace / f"shim-{config}.jsonl"
        self.outcome = BuildOutcome(
            config=config,
            build_dir=self.build_dir,
            prefix=self.prefix,
            shim_log=self.shim_log,
        )

    @property
    def shared_flag(self) -> str:
        return "ON" if self.config == "shared" else "OFF"

    def env(self, **overrides) -> dict:
        return shim_env(self.shim_dir, self.shim_log, self.repo,
                        mode=self.shim_mode, **overrides)

    def configure_argv(self, build_dir: Path, prefix: Path) -> list[str]:
        # Exactly the argv published in the build contract.  The install prefix
        # is fixed here and not changed later: RPATH and the .pc prefix are both
        # bound at configure time, so configuring against one prefix and
        # installing into another yields an install tree that cannot run.
        return [
            "cmake",
            "-S", ".",
            "-B", str(build_dir),
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DBUILD_SHARED_LIBS={self.shared_flag}",
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
        """Build, and on failure build again with concurrency 1.

        The retry is here because without it this stage's score is a scheduling
        outcome rather than a measurement.  A submission whose build graph names
        one output from two targets that nothing orders gets both recipes run at
        once by `make -j`; the graded tree this was written for has five such
        outputs, two of them the `cargo` rule, so two `cargo clean` and two
        `cargo build` ran over one target directory and the staging copy read a
        file the other invocation had just removed.  Measured over nine runs of
        one tree on one image: it failed once.  That once rated 0.155 against
        0.998 for the other eight -- and paid nothing where they did too, since
        module reported `0/N` with `status: ok`, because a build that produces no
        library leaves 4,122 cases with nothing to ask.

        A build that needs `-j1` is a defective build and is still charged, but
        it is charged once, deterministically, by `build/parallel-safe`, which
        reads the duplication out of the generated makefiles and does not depend
        on winning or losing the race.  Grading behaviour on the serial artifacts
        is what keeps the other 4,122 cases about the markdown parser.
        """
        result = vlib.run(
            ["cmake", "--build", str(self.build_dir), "--parallel"],
            cwd=self.repo,
            env=self.env(),
            timeout=BUILD_TIMEOUT,
            log=self.log,
            label=f"build-{self.config}",
        )
        self.outcome.extra["compile-parallel"] = result
        if not result.ok:
            self.log.write(
                f"parallel build failed ({self.config}) rc={result.returncode}; "
                f"retrying with --parallel 1 to find out whether the tree builds "
                f"at all. build/parallel-safe reports the duplication either way.")
            serial = vlib.run(
                ["cmake", "--build", str(self.build_dir), "--parallel", "1"],
                cwd=self.repo,
                env=self.env(CMAKE_BUILD_PARALLEL_LEVEL="1"),
                timeout=BUILD_TIMEOUT,
                log=self.log,
                label=f"build-serial-{self.config}",
            )
            self.outcome.extra["compile-serial"] = serial
            self.log.write(
                f"serial build ({self.config}): "
                f"{'succeeded' if serial.ok else 'also failed'}")
            if serial.ok:
                result = serial
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
    # These run after the graded build has produced its install tree, in
    # throwaway directories, so a failure here costs the cases it belongs to and
    # nothing else.

    def probe_rebuild(self) -> Result:
        """A second build over a finished tree must stay green."""
        result = vlib.run(
            ["cmake", "--build", str(self.build_dir)],
            cwd=self.repo,
            env=self.env(),
            timeout=BUILD_TIMEOUT,
            log=self.log,
            label=f"rebuild-{self.config}",
        )
        self.outcome.extra["rebuild"] = result
        return result

    def probe_reconfigure(self) -> Result:
        """Re-running configure over a populated build directory must succeed."""
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

    def probe_generator(self, scratch: Path, generator: str) -> Result:
        """The project must not have become bound to one generator."""
        out = scratch / f"gen-{generator.replace(' ', '-').lower()}-{self.config}"
        result = vlib.run(
            self.configure_argv(out, self.prefix) + ["-G", generator],
            cwd=self.repo,
            env=self.env(),
            timeout=CONFIGURE_TIMEOUT,
            log=self.log,
            label=f"generator-{generator}-{self.config}",
        )
        self.outcome.extra[f"generator-{generator}"] = result
        shutil.rmtree(out, ignore_errors=True)
        return result

    def probe_testing_off(self, scratch: Path) -> Result:
        """Configuring with testing disabled must also work."""
        out = scratch / f"notest-{self.config}"
        result = vlib.run(
            self.configure_argv(out, self.prefix) + ["-DBUILD_TESTING=OFF"],
            cwd=self.repo,
            env=self.env(),
            timeout=CONFIGURE_TIMEOUT,
            log=self.log,
            label=f"testing-off-{self.config}",
        )
        self.outcome.extra["testing-off"] = result
        shutil.rmtree(out, ignore_errors=True)
        return result

    def probe_insource(self, scratch: Path) -> Result:
        """An in-source configure must still be refused.

        Upstream fails fast on this to protect a user's working tree.  It is a
        deliberate behavior of the build system rather than an accident, so it is
        part of what the migration has to keep.  The check runs against a
        throwaway copy so the graded tree is never polluted by a CMakeCache.
        """
        copy = scratch / f"insource-{self.config}"
        if copy.exists():
            shutil.rmtree(copy)
        vlib.copy_tree(self.repo, copy, skip_names={".git"})
        result = vlib.run(
            ["cmake", "."],
            cwd=copy,
            env=self.env(),
            timeout=CONFIGURE_TIMEOUT,
            log=self.log,
            label=f"insource-{self.config}",
        )
        self.outcome.extra["insource"] = result
        shutil.rmtree(copy, ignore_errors=True)
        return result

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


class ReferenceBuilder:
    """Builds the pinned C reference every behavioural case is graded against."""

    def __init__(self, source: Path, workspace: Path, log: Log) -> None:
        self.source = source
        self.log = log
        self.workspace = workspace

    def build(self, config: str = "shared") -> Path:
        self.log.section(f"build reference ({config})")
        shared = "ON" if config == "shared" else "OFF"
        build_dir = self.workspace / f"ref-build-{config}"
        prefix = self.workspace / f"ref-install-{config}"
        if prefix.exists():
            self.log.write(f"reference ({config}) already built at {prefix}")
            return prefix
        vlib.run(
            [
                "cmake",
                "-S", str(self.source),
                "-B", str(build_dir),
                "-DCMAKE_BUILD_TYPE=Release",
                f"-DBUILD_SHARED_LIBS={shared}",
                f"-DCMAKE_INSTALL_PREFIX={prefix}",
                "-DBUILD_TESTING=OFF",
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


def compile_probe(
    probe_src: Path,
    prefix: Path,
    output: Path,
    log: Log,
    *,
    label: str,
    static: bool = False,
) -> Result:
    """Compile the differential probe against an install tree.

    This is itself one of the graded workflows: if a downstream C consumer cannot
    compile and link against the submission's installed header and library, the
    interface has not been preserved, whatever the library does internally.  The
    probe is built with the real compiler -- it is the verifier's own source, not
    the submission's, and the shim is deliberately not on this PATH.
    """
    argv = [
        "/usr/bin/cc",
        "-std=c11",
        "-O1",
        "-Wall",
        "-o", str(output),
        str(probe_src),
        f"-I{prefix / 'include'}",
    ]
    libdir = prefix / "lib"
    if static:
        argv += ["-DCMARK_STATIC_DEFINE", f"-L{libdir}", "-l:libcmark.a"]
    else:
        argv += [f"-L{libdir}", "-lcmark", f"-Wl,-rpath,{libdir}"]
    return vlib.run(
        argv, env=vlib.base_env(), timeout=300.0, log=log, label=label
    )
