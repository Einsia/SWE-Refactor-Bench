#!/usr/bin/env python3
"""Builds the submission, and builds the four probe tiers against it.

The submission is built the way a Go consumer would build it: `go build ./...`,
`go vet ./...`, `go test ./...`, `go install -trimpath ./cmd/sqlformat`, with the
argv and the environment published in source-contract.json's build_contract,
verbatim.  Nothing is special-cased for grading, with one exception that runs in
the other direction: every Python interpreter name on PATH is replaced by a shim
that refuses to run.  If the migration really happened that changes nothing; if
the build still needs an interpreter it fails and the attempt is recorded.

Three things about how this module is organized are deliberate.

*The graded build happens on a copy, not on the collected tree.*  `GOFLAGS=-mod=mod`
is in the published contract because it is what makes an offline build resolve,
and it also means the toolchain may rewrite go.mod and create go.sum as a side
effect of building.  The audit gates read the tree as submitted, so the tree
as submitted has to survive being built.  Every build in this module therefore
runs against `scratch/submission`, and the collected tree is only ever read.

*The scratch layout is fixed by the probe module, not chosen here.*  probe/go.mod
carries `replace github.com/andialbrecht/sqlparse-go => ../submission`, so the
submission copy and the probe copy have to be siblings under one directory.  That
relative path is in a file the model never sees and cannot be computed at grading
time without rewriting it, so the layout follows the file rather than the reverse.

*Every compile that can fail independently does.*  The four probe tiers, the three
standalone closures and the two install runs are separate compilations in separate
directories.  A submission whose `filters` package does not compile still gets a
truthful answer for the 3800-odd core-tier cases, and that answer -- rather than
one compile error standing in for 3800 unknowns -- is the whole reason the probe
was split into tiers to begin with.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import conform
import vlib
from vlib import Log, Result

# `go build ./...` on a cold cache for a module this size is a minute or two; the
# ceilings are set well above that so a slow machine reports a real failure rather
# than a timeout, and low enough that a submission with a build-time infinite loop
# does not hold the verifier open.
BUILD_TIMEOUT = 1800.0
VET_TIMEOUT = 1200.0
TEST_TIMEOUT = 1800.0
INSTALL_TIMEOUT = 900.0
LIST_TIMEOUT = 600.0
GOFMT_TIMEOUT = 300.0
PROBE_BUILD_TIMEOUT = 900.0

# Every name a Python interpreter could plausibly arrive under.  The version-suffixed
# names matter: a Makefile that calls `python3.11` directly would otherwise walk
# straight past a shim that only intercepts `python3`.
SHIM_TOOLS = (
    "python", "python3", "python2", "python3.9", "python3.10", "python3.11",
    "python3.12", "python3.13", "pypy", "pypy3", "cython", "cython3",
    "pip", "pip3", "pipx", "poetry", "uv", "virtualenv", "conda",
)

# The Go toolchain's own directory comes first because the graded build has to be
# able to find `go` -- unlike a C compiler, which CMake is supposed to locate for
# itself, the toolchain here is the thing under test's whole build system.
REAL_PATH = ("/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:"
             "/usr/sbin:/usr/bin:/sbin:/bin")

MODULE_PATH = "github.com/andialbrecht/sqlparse-go"
CMD_PACKAGE = "./cmd/sqlformat"
BINARY_NAME = "sqlformat"

PROBE_TIERS = ("core", "model", "keywords", "parts")

# Ceilings for the pre-migration build path.  Byte-compiling a tree this size is
# under a second and importing it is milliseconds, so these are generous by two
# orders of magnitude: they are here to bound a submission that loops at import
# time, not to time anything real.
PY_COMPILE_TIMEOUT = 600.0
PY_IMPORT_TIMEOUT = 300.0
PY_SMOKE_TIMEOUT = 300.0

# The package the pre-migration tree is expected to import as, and the two files
# whose presence identifies it.  Both are named because `sqlparse/__init__.py`
# alone is satisfied by an empty directory with an empty file in it, which is a
# shape a Go submission could carry by accident and must not be graded as Python.
PY_PACKAGE = "sqlparse"
PY_MARKERS = ("sqlparse/__init__.py", "sqlparse/sql.py")


def detect_language(repo: Path) -> str:
    """Which builder can measure this tree: "go" or "python".

    `go.mod` decides it, and decides it first.  A tree carrying one is graded as
    the Go module it claims to be even if a `sqlparse/` package is also sitting
    there -- a submission that shipped both is a Go submission with leftovers, and
    the leftovers are stage 1's business, not a reason to grade it as its own
    ancestor.

    Absent `go.mod`, an importable `sqlparse` package makes it the pre-migration
    tree.  That is the only other thing this suite knows how to run, and running it
    is the point: State A is the oracle every frozen expectation was computed from,
    so it is the one tree whose score this stage can check itself against.  A
    submission that is neither -- no `go.mod`, no `sqlparse/` -- is graded as Go and
    fails at `go build`, which is the truthful answer for a tree that is not a Go
    module and not the thing it was supposed to replace either.

    Nothing here is a verdict on whether Python is *allowed*.  It is not: four
    required stage 1 gates (`no-python-implementation`, `no-interpreter-dependency`,
    `go-present`, `go-is-primary`) each independently fail a Python tree, and a
    required gate failing scores the whole submission zero before this image runs.
    This function only decides which set of questions the tree can be asked.
    """
    if (repo / "go.mod").is_file():
        return "go"
    if all((repo / rel).is_file() for rel in PY_MARKERS):
        return "python"
    return "go"


def launcher_text(python: str, tree: Path | None = None) -> str:
    """A `sqlformat` shell launcher that runs sqlparse's own CLI entry point.

    A launcher rather than `python -m sqlparse.cli`, and the reason is that the
    thing being compared is a compiled command: the port ships a binary, so argv[0]
    is a real path and argparse derives `prog` from it.  Reaching the CLI any other
    way would make every CLI signature differ in the program name and nothing else.

    `tree` bakes PYTHONPATH into the script instead of relying on the caller's
    environment.  The graded CLI runner hands its subprocess a scrubbed environment
    that carries no PYTHONPATH -- deliberately, so a submission cannot influence the
    verifier's tooling -- so a launcher that needed one would work at freeze time and
    fail at grading time.  Baking it in makes the launcher self-contained, which is
    what the binary it stands in for is.
    """
    prelude = ""
    if tree is not None:
        prelude = (f'PYTHONPATH="{tree}${{PYTHONPATH:+:$PYTHONPATH}}"\n'
                   f"export PYTHONPATH\n")
    return (
        "#!/bin/sh\n"
        + prelude
        + f"exec {python} -c 'import sys\n"
        f"from {PY_PACKAGE}.cli import main\n"
        "sys.exit(main())' \"$@\"\n"
    )


def tier_launcher_text(python: str, probe_py: Path, tree: Path) -> str:
    """A `probe-<tier>` launcher: the Python half of the probe over `tree`.

    Written where `executor.submission_argv` looks for a compiled tier, and named
    the way that resolver names one, so nothing downstream needs a second code
    path: the driver builds the same argv, the resolver's executable check passes,
    and the wire protocol on the other side of it is the one both halves speak.
    Four launchers rather than one because `tier_ok` is per tier and the cases are
    scored per tier -- one file would make four independent measurements share a
    fate they do not share for a Go submission.

    The interpreter is named by absolute path.  The graded build installs a
    refusing shim under every interpreter name at the front of PATH, so a PATH
    lookup here would find the shim; the same absolute-path reasoning is what lets
    freeze.py run this probe against the reference while the shim is installed.
    """
    return (
        "#!/bin/sh\n"
        f'PYTHONPATH="{tree}${{PYTHONPATH:+:$PYTHONPATH}}"\n'
        "export PYTHONPATH\n"
        f'exec {python} "{probe_py}" "$@"\n'
    )


@dataclass
class BuildOutcome:
    """Everything the graded build produced, and how each step went.

    One object per verifier run.  Unlike the sibling tasks there is no
    static/shared axis to vary -- a Go module has one build -- so the fan-out that
    those tasks express as configurations is expressed here as the several
    independent compilations recorded in `standalone` and `tiers`.
    """

    root: Path
    submission: Path
    prefix: Path
    build: Result | None = None
    vet: Result | None = None
    test: Result | None = None
    install: Result | None = None
    reinstall: Result | None = None
    gofmt: Result | None = None
    deps: Result | None = None
    mods: Result | None = None
    apidump: Result | None = None
    consumer: Result | None = None
    # Keyed by closure name ("model", "keywords", "core") and tier name.  Absent
    # means the compile was never attempted, which is a different thing from
    # attempted and failed, and the cases distinguish the two.
    standalone: dict[str, Result] = field(default_factory=dict)
    tiers: dict[str, Result] = field(default_factory=dict)
    extra: dict[str, Result] = field(default_factory=dict)
    shim_log: Path | None = None
    api: dict | None = None
    #: Which builder produced this outcome -- "go" or "python".  Set from
    #: detect_language() and carried across the process boundary because the
    #: modules that read this outcome have to know which questions its fields can
    #: answer: `build` means `go build ./...` under one and `compileall` plus an
    #: import under the other, and the artifact cases that read an ELF header can
    #: only be asked of the first.
    language: str = "go"

    @property
    def built(self) -> bool:
        return self.build is not None and self.build.ok

    @property
    def installed(self) -> bool:
        return self.install is not None and self.install.ok and self.binary.is_file()

    @property
    def binary(self) -> Path:
        """The installed command.  GOBIN points here, so `go install` lands here."""
        return self.prefix / "bin" / BINARY_NAME

    @property
    def reinstalled_binary(self) -> Path:
        return self.prefix / "bin2" / BINARY_NAME

    @property
    def tier_bin_dir(self) -> Path:
        """Where the compiled tiers land.

        The `probe-` prefix is executor.submission_argv's convention, not a choice
        made here: that resolver looks for `<bin_dir>/probe-<tier>` and returns
        None when it is missing, which is what turns an unbuilt tier into marked
        cases instead of a crash.
        """
        return self.prefix / "probe"

    def tier_binary(self, tier: str) -> Path:
        return self.tier_bin_dir / f"probe-{tier}"

    def tier_ok(self, tier: str) -> bool:
        result = self.tiers.get(tier)
        return (result is not None and result.ok
                and self.tier_binary(tier).is_file())

    def summary(self) -> dict:
        payload = {
            "root": str(self.root),
            "submission": str(self.submission),
            "prefix": str(self.prefix),
            "language": self.language,
            "built": self.built,
            "installed": self.installed,
            "tiers_ok": sorted(t for t in PROBE_TIERS if self.tier_ok(t)),
        }
        for name in ("build", "vet", "test", "install", "reinstall", "gofmt",
                     "deps", "mods", "apidump", "consumer"):
            result = getattr(self, name)
            if result is not None:
                payload[name] = result.brief(limit=8000)
        for label, table in (("standalone", self.standalone), ("tier", self.tiers),
                             ("probe", self.extra)):
            for name, result in sorted(table.items()):
                payload[f"{label}_{name}"] = result.brief(limit=2000)
        return payload

    # -- crossing a process boundary ---------------------------------------
    #
    # The build is one module and everything that reads what it produced is
    # another, so the outcome has to survive a process exit.  summary() cannot do
    # that job even though it looks like it could: it is the report, and it clips
    # each stream to keep the JSON readable.  structure.py searches these streams
    # for specific text -- the go directive in a `go build` error, a vet
    # diagnostic naming a package, `go version -m` output -- and a needle elided
    # from the middle of a clipped log reads exactly like a clean build.  So the
    # handover writes the streams to disk whole and passes paths.
    #
    # The table names are prefixed rather than nested because a Result is restored
    # by name and the three tables have overlapping keys: `standalone:core` and
    # `tier:core` are different compilations of different things.

    #: Fields holding one Result each, by the name they are restored under.
    STEPS = ("build", "vet", "test", "install", "reinstall", "gofmt", "deps",
             "mods", "apidump", "consumer")

    def persist(self, state_dir: Path) -> dict:
        """Write every captured stream to disk and return a path-only record."""
        logs = state_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        record: dict = {
            "root": str(self.root),
            "submission": str(self.submission),
            "prefix": str(self.prefix),
            "shim_log": str(self.shim_log) if self.shim_log else "",
            "api": self.api,
            "language": self.language,
            "steps": {},
        }
        steps: list[tuple[str, Result | None]] = [
            (name, getattr(self, name)) for name in self.STEPS
        ]
        for label, table in (("standalone", self.standalone),
                             ("tier", self.tiers), ("probe", self.extra)):
            steps += [(f"{label}:{name}", result)
                      for name, result in sorted(table.items())]
        for name, result in steps:
            if result is None:
                continue
            stem = name.replace(":", "-").replace("/", "-")
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
            root=Path(record["root"]),
            submission=Path(record["submission"]),
            prefix=Path(record["prefix"]),
            shim_log=Path(record["shim_log"]) if record.get("shim_log") else None,
            api=record.get("api"),
            # Defaulted rather than required: a build state written before this
            # field existed describes a Go build, which is what the default says.
            language=str(record.get("language") or "go"),
        )
        tables = {"standalone": outcome.standalone, "tier": outcome.tiers,
                  "probe": outcome.extra}
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
            label, _, key = name.partition(":")
            if key:
                if label not in tables:
                    raise SystemExit(
                        f"build state names an unknown result table {label!r}; "
                        f"persist() and restore() disagree")
                tables[label][key] = result
            elif label in cls.STEPS:
                setattr(outcome, label, result)
            else:
                # A field persist() wrote and restore() has no home for would be
                # silently dropped, and the case that reads it would fail as
                # though the step had never run.
                raise SystemExit(
                    f"build state names an unknown step {label!r}; persist() and "
                    f"restore() disagree")
        return outcome


def _read_bytes(path: str) -> bytes:
    """A stream persist() wrote, or empty if it is gone.

    Empty rather than an exception: a missing log makes some cases fail for want
    of evidence, which is a worse result for the submission than it deserves but
    still a graded run.  A raise here would take down every case in the module.
    """
    try:
        return Path(path).read_bytes()
    except OSError:
        return b""


def install_shim(shim_src: Path, shim_dir: Path, log: Log) -> Path:
    """Materialize the Python shim under every interpreter name it must intercept.

    The shebang is rewritten to name the real interpreter by absolute path.  This
    is not cosmetic: the shim is installed *as* `python3` at the front of PATH, so
    a `#!/usr/bin/env python3` line would resolve to the shim itself and recurse
    until the process table filled.  The C shims in the sibling tasks never had to
    do this because they do not intercept the language their own shim is written
    in.
    """
    shim_dir.mkdir(parents=True, exist_ok=True)
    target = shim_dir / "pyshim.py"

    source = shim_src.read_text(encoding="utf-8")
    real = os.path.realpath(vlib.PYTHON)
    lines = source.split("\n")
    if lines and lines[0].startswith("#!"):
        lines[0] = f"#!{real}"
    else:  # pragma: no cover - the source in this tree has one
        lines.insert(0, f"#!{real}")
    target.write_text("\n".join(lines), encoding="utf-8")
    target.chmod(0o755)

    for tool in SHIM_TOOLS:
        link = shim_dir / tool
        if link.exists() or link.is_symlink():
            link.unlink()
        # A symlink rather than a wrapper script, so the shim can read argv[0] to
        # learn which interpreter name was asked for.  A `sh -c exec ...` wrapper
        # would replace argv[0] with the shim's own name and the ledger would say
        # "pyshim" for every entry instead of naming the tool the build reached
        # for -- which is the most useful field in the record.
        link.symlink_to(target.name)
    log.write(f"shim: installed {len(SHIM_TOOLS)} interpreter names in {shim_dir} "
              f"(real interpreter {real})")
    return target


def shim_env(shim_dir: Path, shim_log: Path, repo: Path, **overrides) -> dict:
    """An environment in which no Python interpreter can run.

    The Go toolchain directory stays on PATH behind the shim directory: `go` is
    the build system here, and removing it would test nothing but whether the
    submission can find a compiler that is not there.  Everything the contract
    publishes in build_contract.env is set by vlib.base_env already, so a
    submission that reads those variables sees exactly the documented values.
    """
    env = vlib.base_env(
        PATH=f"{shim_dir}:{REAL_PATH}",
        PYSHIM_LOG=str(shim_log),
        PYSHIM_REPO=str(repo),
        **overrides,
    )
    return env


class Builder:
    """Drives the graded build of one submission.

    Construction lays out the scratch tree and copies the submission into it;
    nothing is compiled until a method is called.  `run_all` performs the graded
    sequence and stops early only where continuing would be meaningless -- a
    module that does not build cannot be installed, but a module that fails vet
    can still be installed and graded, so vet does not gate anything.
    """

    def __init__(self, repo: Path, workspace: Path, shim_dir: Path, log: Log,
                 *, tests_dir: Path) -> None:
        self.repo = repo
        self.log = log
        self.tests_dir = tests_dir
        self.shim_dir = shim_dir

        # The probe module's `replace` directive is `../submission`, so these two
        # names are not free choices -- see the module docstring.
        self.root = workspace / "gobuild"
        self.submission = self.root / "submission"
        self.probe_dir = self.root / "probe"
        self.apidump_dir = self.root / "apidump"
        self.consumer_dir = self.root / "consumer"
        self.prefix = workspace / "goprefix"
        self.scratch = workspace / "goscratch"
        self.shim_log = workspace / "pyshim.jsonl"

        self.outcome = BuildOutcome(
            root=self.root,
            submission=self.submission,
            prefix=self.prefix,
            shim_log=self.shim_log,
        )

    # -- setup ------------------------------------------------------------

    def prepare(self) -> int:
        """Copy the submission and the verifier's own Go modules into scratch.

        Returns the number of files copied from the submission.  `.git` is skipped
        because the baseline snapshot's history is not part of what was submitted,
        and a git directory in the tree would also trip the forbidden-paths gate
        against the copy rather than against the tree the gate means.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        (self.prefix / "bin").mkdir(parents=True, exist_ok=True)
        (self.prefix / "bin2").mkdir(parents=True, exist_ok=True)
        (self.prefix / "probe").mkdir(parents=True, exist_ok=True)
        self.scratch.mkdir(parents=True, exist_ok=True)

        if self.submission.exists():
            shutil.rmtree(self.submission)
        count = vlib.copy_tree(self.repo, self.submission, skip_names={".git"})
        self.log.write(f"build: copied {count} file(s) to {self.submission}")

        for src, dst in ((self.tests_dir / "probe", self.probe_dir),
                         (self.tests_dir / "apidump", self.apidump_dir)):
            if dst.exists():
                shutil.rmtree(dst)
            # Copied rather than symlinked or built in place: `go build` writes
            # go.sum into the module directory, and tests/ is mounted read-only in
            # the verifier image.
            vlib.copy_tree(src, dst)

        # The conformance consumer is generated here rather than copied, because it
        # is a pure function of source-contract.json and nothing else.  Generating
        # it at grading time means there is no second copy that can disagree with
        # the contract that shipped, and no generated Go in the task tree.
        contract = json.loads(
            (self.tests_dir / "source-contract.json").read_text(encoding="utf-8"))
        asserted, packages = conform.write(contract, self.consumer_dir)
        self.log.write(
            f"build: staged probe and apidump, generated consumer "
            f"({asserted} assertions over {packages} packages) under {self.root}")
        return count

    def env(self, **overrides) -> dict:
        """The graded build environment: contract variables, plus no interpreter.

        GOBIN is set so `go install` has a defined destination.  The contract says
        the install "produces bin/sqlformat", and GOBIN is the only thing that
        decides where `go install` puts a binary -- without it the destination
        depends on GOPATH, which would make the published sentence describe the
        verifier's environment rather than the submission's behavior.
        """
        settings = {"GOBIN": str(self.prefix / "bin")}
        settings.update(overrides)
        return shim_env(self.shim_dir, self.shim_log, self.repo, **settings)

    def go(self, args: list[str], *, label: str, timeout: float,
           cwd: Path | None = None, env: dict | None = None) -> Result:
        return vlib.run(
            ["go", *args],
            cwd=cwd or self.submission,
            env=env or self.env(),
            timeout=timeout,
            log=self.log,
            label=label,
        )

    def go_module(self, module: Path, args: list[str], *, label: str,
                  timeout: float, output: Path | None = None) -> Result:
        """Run the toolchain in one of the verifier's own modules.

        Distinct from `go` in two ways that both matter.  The environment is
        `vlib.base_env`, so the interpreter shim is not on this PATH: the probe and
        the consumer are Go source, nothing about them should depend on that, and a
        PATH that differs between the submission's build and the verifier's is one
        less thing that can silently couple them.  And `-mod=mod` is set, so the
        toolchain may resolve the local `replace` without a checksum -- which is
        what makes these modules build offline against a tree that has no go.sum
        entry anywhere.
        """
        argv = ["go", *args] if output is None else \
            ["go", args[0], "-o", str(output), *args[1:]]
        return vlib.run(
            argv,
            cwd=module,
            env=vlib.base_env(GOFLAGS="-mod=mod"),
            timeout=timeout,
            log=self.log,
            label=label,
        )

    # -- the graded sequence ---------------------------------------------

    def build(self) -> Result:
        result = self.go(["build", "./..."], label="go-build",
                         timeout=BUILD_TIMEOUT)
        self.outcome.build = result
        return result

    def vet(self) -> Result:
        result = self.go(["vet", "./..."], label="go-vet", timeout=VET_TIMEOUT)
        self.outcome.vet = result
        return result

    def test(self) -> Result:
        # The submission's own tests.  A port that carries none is not penalized
        # here -- `go test ./...` over a module with no test files exits 0 and
        # prints "no test files" per package -- but a port whose own tests fail is,
        # because a failing test suite is a claim the submission itself is making
        # about being broken.
        result = self.go(["test", "./..."], label="go-test", timeout=TEST_TIMEOUT)
        self.outcome.test = result
        return result

    def install(self) -> Result:
        result = self.go(["install", "-trimpath", CMD_PACKAGE],
                         label="go-install", timeout=INSTALL_TIMEOUT)
        self.outcome.install = result
        return result

    def reinstall(self) -> Result:
        """A second install into a second GOBIN, for the reproducibility case.

        The second install gets a *fresh* build cache.  Left to share the first
        one, `go install` recognizes the same action IDs and re-links the cached
        objects, so the comparison would confirm that Go's content-addressed cache
        works rather than anything about the submission.  With an empty cache every
        package is compiled again from source, and a byte-identical result then
        says what the case claims: nothing that varies between two runs -- a
        timestamp, a hostname, a counter, an absolute path that -trimpath did not
        reach -- was compiled into the binary.

        It is also a second offline build from nothing, which is the other property
        the contract lists: GOPROXY is off and the module cache is a scratch
        directory, so a module graph that needed the network could not resolve.
        """
        env = self.env(
            GOBIN=str(self.prefix / "bin2"),
            GOCACHE=str(self.scratch / "gocache-second"),
        )
        result = vlib.run(
            ["go", "install", "-trimpath", CMD_PACKAGE],
            cwd=self.submission,
            env=env,
            timeout=INSTALL_TIMEOUT,
            log=self.log,
            label="go-install-again",
        )
        self.outcome.reinstall = result
        return result

    def gofmt(self) -> Result:
        # `gofmt -l` prints the files it would change and exits 0 either way, so
        # the case reads stdout rather than the status.
        result = vlib.run(
            ["gofmt", "-l", "."],
            cwd=self.submission,
            env=self.env(),
            timeout=GOFMT_TIMEOUT,
            log=self.log,
            label="gofmt-l",
        )
        self.outcome.gofmt = result
        return result

    def list_deps(self) -> Result:
        # The import closure of everything the module builds, one package per
        # line.  A third-party or C dependency shows up here as a line that is
        # neither stdlib nor the module itself.
        result = self.go(["list", "-deps", "./..."], label="go-list-deps",
                         timeout=LIST_TIMEOUT)
        self.outcome.deps = result
        return result

    def list_modules(self) -> Result:
        result = self.go(["list", "-m", "all"], label="go-list-modules",
                         timeout=LIST_TIMEOUT)
        self.outcome.mods = result
        return result

    def list_imports(self) -> Result:
        """What the submission's own packages import directly, per package.

        Deliberately not `-deps`.  The transitive closure of any real Go program
        contains `unsafe` and `syscall` -- `os` imports the latter, `sync/atomic`
        the former -- so asking whether a forbidden name appears anywhere in the
        closure answers "does this program use the standard library", not "does
        this program shell out".  The direct imports of the submission's own
        packages are the question the forbidden list is actually about, and they
        are also the toolchain's independent second opinion on audit.py's hand
        parser: the parser reads text and can be fooled by a file the compiler
        never sees, while this sees exactly what the build compiled.

        Test imports are listed too, under a `[test]` suffix.  A test that shells
        out to Python is not production behavior, and it is still a live path to
        the old implementation sitting in the tree.
        """
        template = ('{{.ImportPath}}\t{{join .Imports " "}}\n'
                    '{{.ImportPath}}[test]\t{{join .TestImports " "}} '
                    '{{join .XTestImports " "}}')
        result = self.go(["list", "-f", template, "./..."],
                         label="go-list-imports", timeout=LIST_TIMEOUT)
        self.outcome.extra["go-list-imports"] = result
        return result

    def read_buildinfo(self) -> Result:
        """Ask the toolchain what it recorded in the installed binary.

        This is deliberately redundant with golib.find_buildinfo, which decodes
        the same blob out of the ELF by hand.  Two independent readers of one
        artifact is the point: `go version -m` is authoritative but only exists
        because a Go toolchain is present, while the hand decoder works on a file
        with no toolchain nearby and can be pointed at a binary the submission
        merely shipped.  The cases read the hand decoder and use this to
        cross-check it; a disagreement between them is reported as a disagreement
        rather than silently resolved in either direction, because it means the
        binary's provenance record is not what either reader thinks it is.
        """
        result = vlib.run(
            ["go", "version", "-m", str(self.outcome.binary)],
            cwd=self.submission,
            env=self.env(),
            timeout=LIST_TIMEOUT,
            log=self.log,
            label="go-version-m",
        )
        self.outcome.extra["go-version-m"] = result
        return result

    def run_all(self) -> BuildOutcome:
        self.log.section("build submission")
        self.prepare()
        if not self.build().ok:
            self.log.write("go build failed; install and probe tiers will be "
                           "skipped, structural cases still run")
            # vet and gofmt are still worth running: both work on source and both
            # produce findings that help explain why the build failed.
            self.vet()
            self.gofmt()
            return self.outcome
        self.vet()
        self.gofmt()
        self.test()
        self.install()
        if self.outcome.installed:
            self.reinstall()
            self.read_buildinfo()
        self.list_deps()
        self.list_modules()
        self.list_imports()
        return self.outcome

    # -- independent compilations -----------------------------------------
    #
    # Each of these is its own compile closure, run after the graded build, so a
    # failure costs the cases it belongs to and nothing else.

    def standalone(self, name: str, packages: list[str]) -> Result:
        """Build a subset of the module without the rest of it.

        This grades State A's layering, which the contract publishes in
        go_contract.standalone_closures: sqlparse.tokens and sqlparse.utils import
        nothing internal, sqlparse.sql imports tokens and utils, sqlparse.keywords
        imports tokens.  A consumer who wants the token model without the
        formatter can have it in State A, and must still be able to have it.

        The build alone is not the whole property -- `go build ./sql ./tokens`
        succeeds even if sql imports the entire module, because Go compiles the
        transitive closure without complaint.  So the closure is read back with
        `go list -deps` and checked by structure.py against the published allow
        list.  This method records both.
        """
        result = self.go(["build", *packages], label=f"standalone-{name}",
                         timeout=BUILD_TIMEOUT)
        self.outcome.standalone[name] = result
        if result.ok:
            listing = self.go(["list", "-deps", *packages],
                              label=f"standalone-deps-{name}",
                              timeout=LIST_TIMEOUT)
            self.outcome.standalone[f"{name}-deps"] = listing
        return result

    def standalone_all(self, closures: dict) -> None:
        self.log.section("standalone closures")
        for name in sorted(closures):
            entry = closures[name]
            self.standalone(name, list(entry["build"]))

    def compile_tier(self, tier: str) -> Result:
        """Compile one probe tier against the submission.

        This is itself one of the graded workflows, and the strongest single
        structural signal in the task: the probe is an ordinary downstream Go
        consumer, written against the published interface, and it is compiled from
        the verifier's own source with a `replace` pointing at the submission.  If
        it does not compile, the interface has not been preserved -- whatever the
        library does internally, no consumer written against State A's API can
        call it.

        Each tier is built separately and into its own output path, because each
        imports a different set of packages: `core` needs only the root package, so
        a submission whose `filters` package is broken still answers every
        core-tier case.
        """
        result = self.go_module(
            self.probe_dir, ["build", f"./{tier}"],
            label=f"probe-{tier}", timeout=PROBE_BUILD_TIMEOUT,
            output=self.outcome.tier_binary(tier))
        self.outcome.tiers[tier] = result
        return result

    def compile_tiers(self) -> dict[str, Result]:
        self.log.section("compile probe tiers")
        for tier in PROBE_TIERS:
            self.compile_tier(tier)
        ok = [t for t in PROBE_TIERS if self.outcome.tier_ok(t)]
        self.log.write(f"probe tiers built: {', '.join(ok) if ok else 'none'}")
        return self.outcome.tiers

    def compile_consumer(self) -> Result:
        """Type-check the whole published API from outside the module.

        The probe tiers call the API, so they type-check the parts of it they call;
        apidump reads signatures out of the submission's own source, so it grades
        what the submission says about itself.  Neither of those covers a symbol no
        probe names -- 40 of the contract's 155 do not appear in any tier -- and
        neither catches a declared type that is spelled right and composes wrong.
        `sql.NewGroup(Kind, []*Node) *Node` and a `KindWhere` that is not a `Kind`
        can coexist in a tree apidump calls conforming.

        The consumer is one file of `var _ T = pkg.Symbol` declarations generated
        from the contract at image build time.  It is compiled, not run: every
        assertion is resolved by the type checker, so a successful build *is* the
        result and there is no binary to keep.
        """
        self.log.section("compile conformance consumer")
        result = self.go_module(
            self.consumer_dir, ["build", "./..."],
            label="consumer", timeout=PROBE_BUILD_TIMEOUT)
        self.outcome.consumer = result
        if result.ok:
            self.log.write("consumer: the published API type-checks from outside")
        return result

    def dump_api(self) -> dict | None:
        """Read the submission's exported API out of its source.

        Deliberately source-level rather than reflective.  A submission that does
        not compile still has an interface, and grading the interface half against
        a tree that failed to build is both possible and fair -- the two halves
        measure different things, and one failing should not zero the other.
        """
        self.log.section("dump exported API")
        tool = self.scratch / "apidump"
        built = self.go_module(
            self.apidump_dir, ["build", "."],
            label="apidump-build", timeout=PROBE_BUILD_TIMEOUT, output=tool)
        self.outcome.extra["apidump-build"] = built
        if not built.ok:
            # The dumper is verifier code, so this is a broken verifier rather
            # than a bad submission.  It is reported as such by the driver, which
            # checks this result before it scores the API cases.
            self.log.write("apidump did not build; API cases cannot be graded")
            return None

        result = vlib.run(
            [str(tool), str(self.submission)],
            env=vlib.base_env(),
            timeout=LIST_TIMEOUT,
            log=self.log,
            label="apidump-run",
            full_capture=True,
        )
        self.outcome.apidump = result
        if not result.ok:
            return None
        try:
            self.outcome.api = json.loads(result.stdout)
        except (ValueError, UnicodeDecodeError) as exc:
            self.log.write(f"apidump output did not parse: {exc}")
            return None
        self.log.write(f"apidump: {len(self.outcome.api)} package(s)")
        return self.outcome.api


class PyBuilder:
    """Makes the pre-migration tree runnable, so its behaviour can be measured.

    This exists because of a property of the stage rather than a wish to be lenient
    about Python.  Every frozen expectation in this suite was computed by running
    the Python half of the probe against sqlparse 0.5.3, so State A is not merely
    one more submission -- it is the oracle, and the score it gets here is the
    stage's own calibration.  With only a Go builder, State A failed to build and
    every behavioural case in the suite was recorded as failed, which made the
    corpus unfalsifiable: a submission scoring badly and the oracle scoring badly
    looked identical, and a shortfall stopped being readable as a behavioural
    difference.

    Nothing here weakens the Go path, which is untouched, and nothing here decides
    that a Python submission may pass.  It may not: four required stage 1 gates fail
    a Python tree, and a required gate failing scores the whole submission zero
    before this image runs.  What this builder produces is a measurement, and for a
    Python tree the measurement's own meaning is "this is what the retired
    implementation scores on its own corpus" -- which is 1.0, and has to be, or the
    scale has no zero point.

    Three artifacts, each standing in for one the Go builder produces:

      build      `compileall` over the tree, then an import of the package.  Both,
                 because compileall is a parser and would pass a module whose
                 import raises -- `go build` type-checks, so the analogue has to
                 execute the package body.
      install    `bin/sqlformat`, a launcher over the submission's own CLI entry
                 point, written where GOBIN would have put the command.
      tiers      `probe/probe-<tier>`, four launchers over the Python half of the
                 probe with the submission on PYTHONPATH, written where
                 executor.submission_argv looks for a compiled tier.

    The last one is why the driver needs no second code path for the probe: the
    resolver finds an executable at the path it already builds, and what answers on
    the other end speaks the wire protocol both halves of the pair speak.
    """

    def __init__(self, repo: Path, workspace: Path, log: Log, *,
                 probe_py: Path) -> None:
        self.repo = repo
        self.log = log
        self.probe_py = probe_py
        # The same layout the Go builder uses, and for the same reason: the graded
        # build runs against a copy so the collected tree survives being built, and
        # the audit gates read the tree as submitted.
        self.root = workspace / "gobuild"
        self.submission = self.root / "submission"
        self.prefix = workspace / "goprefix"
        self.scratch = workspace / "goscratch"
        self.shim_log = workspace / "pyshim.jsonl"
        self.outcome = BuildOutcome(
            root=self.root, submission=self.submission, prefix=self.prefix,
            shim_log=self.shim_log, language="python")

    # -- the environment ---------------------------------------------------

    def env(self, **overrides) -> dict:
        """A scrubbed environment with the submission importable.

        `PYTHONPYCACHEPREFIX` sends every `.pyc` compileall writes to scratch
        instead of into the tree.  `PYTHONDONTWRITEBYTECODE` does not cover this --
        compileall's whole job is to write bytecode and it does so through an
        explicit call that the flag does not reach -- and bytecode under the
        submission would be the build leaving artifacts in a tree later modules
        inventory.
        """
        env = vlib.base_env(**overrides)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (f"{self.submission}:{existing}" if existing
                             else str(self.submission))
        env["PYTHONPYCACHEPREFIX"] = str(self.scratch / "pycache")
        return env

    # -- the phases --------------------------------------------------------

    def prepare(self) -> int:
        """Copy the submission into scratch.  Returns the number of files copied."""
        self.root.mkdir(parents=True, exist_ok=True)
        (self.prefix / "bin").mkdir(parents=True, exist_ok=True)
        (self.prefix / "probe").mkdir(parents=True, exist_ok=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        if self.submission.exists():
            shutil.rmtree(self.submission)
        count = vlib.copy_tree(self.repo, self.submission, skip_names={".git"})
        self.log.write(f"build: copied {count} file(s) to {self.submission}")
        return count

    def build(self) -> Result:
        """Byte-compile the tree, then import the package.

        Recorded as `outcome.build` because that is the field every reader of a
        build outcome asks for "did the submission's source survive its toolchain",
        and for a Python tree these two commands are that question.  The import is
        the half that matters: a syntax error is rare in a tree that was running
        yesterday, and a module that raises on import is what a broken port looks
        like.
        """
        self.log.section("compile the submission")
        compiled = vlib.run(
            [vlib.PYTHON, "-m", "compileall", "-q", "-f", str(self.submission)],
            cwd=self.submission, env=self.env(), timeout=PY_COMPILE_TIMEOUT,
            log=self.log, label="py-compileall")
        self.outcome.extra["py-compileall"] = compiled
        if not compiled.ok:
            self.outcome.build = compiled
            return compiled

        imported = vlib.run(
            [vlib.PYTHON, "-c",
             f"import {PY_PACKAGE}, sys; "
             f"sys.stdout.write({PY_PACKAGE}.__version__)"],
            # cwd is scratch, not the submission: from inside the tree the package
            # would import off the current directory whether PYTHONPATH named it or
            # not, and the point is to prove the copy this builder staged imports.
            cwd=self.scratch, env=self.env(), timeout=PY_IMPORT_TIMEOUT,
            log=self.log, label="py-import")
        self.outcome.extra["py-import"] = imported
        self.outcome.build = imported
        if imported.ok:
            self.log.write(f"build: {PY_PACKAGE} imports, reports version "
                           f"{imported.text().strip()!r}")
        # Bytecode must not have landed in the tree.  Asserted rather than trusted:
        # PYTHONPYCACHEPREFIX is one variable away from being ineffective, and the
        # failure is silent -- a later module inventories the tree and reports files
        # the submission did not write.
        stray = sorted(p for p in self.submission.rglob("__pycache__"))
        if stray:
            raise SystemExit(
                f"verifier defect: byte-compiling wrote {len(stray)} __pycache__ "
                f"director{'y' if len(stray) == 1 else 'ies'} into the submission "
                f"copy despite PYTHONPYCACHEPREFIX ({stray[0]}). A later module "
                f"inventories that tree, so this would be reported as files the "
                f"submission shipped.")
        return imported

    def install(self) -> Result:
        """Write `bin/sqlformat` and prove it runs.

        The install check is "the command exists and works", so writing the file is
        not the measurement -- a launcher naming an entry point the tree does not
        have would be written successfully and fail on first use.  It is invoked
        with `--help`, which exercises the import, the entry point and argparse
        without depending on any document.
        """
        self.log.section("install the command")
        launcher = self.outcome.binary
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(launcher_text(vlib.PYTHON, self.submission),
                            encoding="utf-8")
        launcher.chmod(0o755)
        result = vlib.run(
            [str(launcher), "--help"],
            cwd=self.scratch, env=vlib.base_env(), timeout=PY_SMOKE_TIMEOUT,
            log=self.log, label="py-install-smoke")
        self.outcome.install = result
        self.outcome.extra["py-install-smoke"] = result
        if result.ok:
            self.log.write(f"install: {launcher.name} runs "
                           f"({launcher.stat().st_size} bytes)")
        return result

    def compile_tiers(self) -> dict[str, Result]:
        """Write the four tier launchers and put one real request through each.

        `version` is the request, and it is the right one: it imports the
        submission's package and reports its version, so a launcher that answers it
        has proved the shell script, the interpreter path, probe.py, the import and
        the version all at once.  A tier whose smoke test fails is left not-ok and
        costs its own cases and nothing else, exactly as an uncompiled Go tier does.
        """
        self.log.section("stage the probe tiers")
        bin_dir = self.outcome.tier_bin_dir
        bin_dir.mkdir(parents=True, exist_ok=True)
        for tier in PROBE_TIERS:
            path = self.outcome.tier_binary(tier)
            path.write_text(
                tier_launcher_text(vlib.PYTHON, self.probe_py, self.submission),
                encoding="utf-8")
            path.chmod(0o755)
            result = vlib.run(
                [str(path), "--docs", str(self.scratch),
                 "--documents", str(self._empty_documents()),
                 "--spec", str(self._empty_spec()), "--tier", tier],
                cwd=self.scratch, env=vlib.base_env(), timeout=PY_SMOKE_TIMEOUT,
                log=self.log, label=f"py-tier-{tier}",
                stdin_data=b"smoke\tversion\n")
            answered = result.ok and b"\tok\t" in result.stdout
            if not answered and result.ok:
                # A zero exit with no answer is the failure mode a plain rc check
                # would miss, so the recorded Result is rewritten to say so: the
                # readers of this table branch on `.ok`.
                result = Result(
                    argv=result.argv, cwd=result.cwd, returncode=1,
                    stdout=result.stdout,
                    stderr=result.stderr + b"\nprobe answered without status ok",
                    duration=result.duration, timed_out=result.timed_out)
            self.outcome.tiers[tier] = result
        ok = sorted(t for t in PROBE_TIERS if self.outcome.tier_ok(t))
        self.log.write(f"tiers: {len(ok)}/{len(PROBE_TIERS)} answered "
                       f"({', '.join(ok) if ok else 'none'})")
        return self.outcome.tiers

    def run_all(self) -> BuildOutcome:
        """prepare, build, install, tiers.  Returns the outcome either way."""
        self.prepare()
        self.build()
        self.install()
        self.compile_tiers()
        return self.outcome

    # -- the smoke test's inputs -------------------------------------------
    #
    # probe.py requires --documents and --spec and reads both at startup, before
    # any request.  The smoke test asks an op that touches neither, so the files
    # only have to parse.  Written empty rather than pointed at the real ones so
    # the smoke test cannot depend on the grading inputs being where it guessed.

    def _empty_documents(self) -> Path:
        path = self.scratch / "smoke-documents.json"
        if not path.is_file():
            path.write_text(
                json.dumps({"schema": "swerefactor-documents-v1", "count": 0,
                            "documents": []}) + "\n", encoding="utf-8")
        return path

    def _empty_spec(self) -> Path:
        path = self.scratch / "smoke-spec.json"
        if not path.is_file():
            path.write_text(json.dumps({}) + "\n", encoding="utf-8")
        return path


class ReferenceInstaller:
    """Makes the pinned Python reference importable, for the Python probe half.

    Unlike the sibling tasks there is nothing to compile: the reference is a pure
    Python package, and "installing" it means putting its directory somewhere
    PYTHONPATH can name.  It is extracted from the same pinned tarball the agent
    started from, and it is extracted rather than pip-installed so no network,
    build backend or interpreter-version resolution enters the picture.

    This runs at image build time only.  The frozen expectations it produces are
    what the submission is graded against, so the reference is not present at all
    when a submission is scored -- which is the point: a submission cannot consult
    an oracle that is not in the image.
    """

    def __init__(self, tarball: Path, workspace: Path, log: Log) -> None:
        self.tarball = tarball
        self.workspace = workspace
        self.log = log
        self.root = workspace / "reference"

    def install(self) -> Path:
        """Unpack the reference and return the directory to put on PYTHONPATH."""
        self.log.section("unpack Python reference")
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        vlib.run(
            ["tar", "-xzf", str(self.tarball), "-C", str(self.root),
             "--strip-components=1"],
            env=vlib.base_env(),
            timeout=300.0,
            log=self.log,
            label="unpack-reference",
            check=True,
        )
        package = self.root / "sqlparse"
        if not (package / "__init__.py").is_file():
            raise SystemExit(
                f"verifier corrupted: no sqlparse package under {self.root}"
            )
        self.log.write(f"reference: sqlparse unpacked at {package}")
        return self.root


def reference_env(reference_root: Path, **overrides) -> dict:
    """An environment in which the pinned reference is the importable sqlparse.

    PYTHONPATH is prepended rather than replaced so the standard library still
    resolves, and the reference root goes first so a stray installed sqlparse
    could not shadow the pinned one.  There is no installed sqlparse in the image;
    the ordering is here so that if one ever appeared, the pinned tree would still
    win and the oracle would still be the version the task is pinned to.
    """
    env = vlib.base_env(**overrides)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (f"{reference_root}:{existing}" if existing
                         else str(reference_root))
    return env
