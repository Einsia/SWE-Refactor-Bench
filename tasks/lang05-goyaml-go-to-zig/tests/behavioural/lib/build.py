#!/usr/bin/env python3
"""Builds the submission the way the instruction says it will be built.

`zig build`, at the repository root, with no arguments, offline, with no Go
toolchain reachable from anywhere in the build.  The argv is the one published in
source-contract.json, verbatim -- nothing is special-cased for grading.

Three things about the environment are deliberate and run against the submission
rather than for it:

* The Zig package cache is empty and outside the tree.  A submission that
  declared a dependency in build.zig.zon fails here with Zig's own message about
  a missing package, which is more useful than a network error at a random point.
* Every Go binary name on PATH is replaced by a tripwire.  The build has no
  legitimate use for one, so any invocation is both refused and recorded.
* The build runs with the cache directories pointed outside the repository, the
  same way the agent image sets them.  A submission that hard-codes a cache path
  inside the tree is then visible as a directory this verifier did not create.

`zig-out/` and the caches are discarded before building.  Whatever the agent's
container left there was produced by a build this verifier did not supervise,
possibly with network access and certainly without the tripwire.

-- Two drivers -----------------------------------------------------------------

All of the above describes one of two build drivers, and it is the one every
submission is expected to resolve to.  The other builds the repository as it was
handed over, with Go.

The reason is that this stage grades behaviour.  A repository rewrite keeps the
library's functionality and changes what it is written in, so the question here is
whether the library still answers the way it did -- and the reference repository,
which trivially does, has to be able to score full marks on the stage built from
it.  A stage that could only build Zig would score the original at zero on every
behavioural module, which says nothing about any submission and makes the gate
meaningless -- and under a gate set at full marks it would make stage 3
unreachable for every submission, since the reference itself could not clear it.

Resolving to the Go driver is not a way to pass.  It requires go.mod in the tree,
which fails stage 1's `no-go-sources` and `no-go-in-build` gates -- both required,
and a required gate failure zeroes the submission whatever this stage measured.
What the second driver buys is that every point lost here is a behavioural
difference and nothing else.

The Go toolchain's lifetime follows from that.  It is in the image, off PATH.
Before a Zig build it is deleted outright and its absence is a scored case, so a
Zig submission is graded in exactly the container it was before.  For a Go build
it is put on PATH, and deleted the moment the build and its probes are done -- so
every module that drives the built binary, on either path, runs with no Go
compiler anywhere.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import vlib
from vlib import Log, Result

BUILD_TIMEOUT = 3600.0
STEP_TIMEOUT = 1800.0
QUERY_TIMEOUT = 300.0

# Every name a Go toolchain could plausibly arrive through.  `go` is the one that
# matters; the rest are here because "no go" is not the same claim as "no Go
# toolchain", and a build step spelled `gofmt -l .` should be reported as what it
# is rather than as a missing file.
SHIM_TOOLS = (
    "go", "gofmt", "godoc", "gopls", "gccgo", "go1.23.4", "gorun",
    "goimports", "golangci-lint", "dlv", "cgo",
)

# The real toolchain.  The tripwire directory is prepended to this, never
# substituted for it: the build needs zig, and zig needs nothing else -- it ships
# its own linker and its own libc sources.
REAL_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Build output, by the names the instruction says are build output.  Removed
# before the graded build and reported if a second one appears somewhere else.
BUILD_OUTPUT_NAMES = ("zig-out", ".zig-cache", "zig-cache")

# The same, for the Go driver: `bin/` is where its install step is told to put the
# probe.  Kept separate rather than merged into one list, because "the build wrote
# a path into the tree that is not its own output" is a real finding and widening
# the allowance for one driver would weaken it for the other.
GO_BUILD_OUTPUT_NAMES = ("bin",)

# Where the Go toolchain lives in the verifier image, and what else it brings.
# Off PATH: `resolve_driver` decides whether a build ever sees it, and the Zig
# path deletes all of this before building.
GOROOT = Path("/usr/local/go")
GOMODCACHE = Path("/opt/gomodcache")
GO_LEFTOVERS = (Path("/root/go"), Path("/root/.cache/go-build"))


def resolve_driver(repo: Path, contract: dict) -> dict:
    """Which of the contract's build drivers this tree presents.

    The first driver whose `detect` matches wins; the last has no `detect` and is
    the fallback.  Detection reads the tree and nothing else -- no environment
    variable, no flag, no marker file a submission could write to pick its own
    driver -- so two modules looking at the same snapshot always resolve the same
    way, which is why this needs no state passed between them.

    A `detect` block may name two predicates, and both must hold: every one is an
    "any" within itself and a conjunction against the others.

      any_path_exists   at least one of these relative paths exists
      any_glob_matches  at least one file matches one of these globs, at the
                        repository root

    Two predicates rather than one, because `go.mod` alone is not the question.
    A *finished* Zig port that forgot to delete go.mod would route to the Go
    driver on that file: `go build` would then fail for want of the sources the
    port had correctly removed, and `build` is a required module, so a complete
    rewrite would score zero over one leftover file.  What the driver means to
    ask is "is this still the Go module", and a go.mod beside no Go source is not
    a Go module -- it is a stray file, which is stage 1's `no-go-sources` gate to
    judge and not this function's.
    """
    drivers = ((contract.get("graded_builds") or {}).get("drivers")) or []
    if not drivers:
        raise RuntimeError(
            "source-contract.json declares no graded_builds.drivers; there is no "
            "build to run"
        )
    known = {"any_path_exists", "any_glob_matches"}
    for entry in drivers:
        detect = entry.get("detect") or {}
        unknown = sorted(set(detect) - known)
        if unknown:
            # A predicate this function does not implement would otherwise be
            # ignored, and ignoring one half of a conjunction makes detection
            # *wider* than the contract says -- the direction that misroutes.
            raise RuntimeError(
                f"driver {entry.get('id')!r} declares detect predicate(s) "
                f"{unknown} that resolve_driver does not implement; "
                f"known: {sorted(known)}"
            )
        if not detect:
            # The fallback.  Only legal as the last entry, and the contract's own
            # round-trip check enforces that; asserted here too because this
            # function is what would silently stop reaching the later drivers.
            if entry is not drivers[-1]:
                raise RuntimeError(
                    f"driver {entry.get('id')!r} declares no detect but is not "
                    f"last; the drivers after it are unreachable"
                )
            return entry
        tests = []
        if detect.get("any_path_exists"):
            tests.append(any((repo / rel).exists()
                             for rel in detect["any_path_exists"]))
        if detect.get("any_glob_matches"):
            tests.append(any(any(repo.glob(pat))
                             for pat in detect["any_glob_matches"]))
        if tests and all(tests):
            return entry
    return drivers[-1]


# Trees that must resolve to a named driver, and the driver each must resolve to.
# Checked by `check_drivers` at image build time, because detection is the one
# decision in this suite that cannot be re-made later: it picks the compiler, the
# artefact path, the allowed build outputs and which provenance cases are scored,
# and every module downstream reads the answer rather than the tree.
#
# The third row is why this table exists.  With the Go driver detecting on `go.mod`
# alone it resolved to `state-a-go`, and a finished Zig port that forgot one file
# unscored three provenance checks it should have been paid for, which under an
# all-or-nothing stage is the whole stage.  A table of five trees is cheap; the bug
# was not visible in any single-tree run, because State A and a clean port both resolve
# correctly and the trap is only in the tree between them.
DRIVER_RESOLUTION_CASES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("State A: the Go module, with the Zig skeleton beside it",
     ("go.mod", "go.sum", "yaml.go", "parserc.go", "build.zig", "build.zig.zon",
      "src/main.zig", "probe/probe.go", "cmd/yaml-probe/main.go"), "state-a-go"),
    ("a finished Zig port",
     ("build.zig", "build.zig.zon", "src/main.zig", "src/parser.zig"),
     "state-b-zig"),
    ("a finished Zig port that forgot to delete go.mod",
     ("go.mod", "go.sum", "build.zig", "build.zig.zon", "src/main.zig"),
     "state-b-zig"),
    ("a Zig port with one stray .go file and no go.mod",
     ("build.zig", "src/main.zig", "notes.go"), "state-b-zig"),
    ("a tree with neither toolchain's marker",
     ("README.md",), "state-b-zig"),
)


def check_drivers(contract: dict) -> list[str]:
    """Resolve `DRIVER_RESOLUTION_CASES` and report every row that disagrees.

    Returns problem strings rather than raising, so the caller can report all of
    them at once alongside its other findings.
    """
    import tempfile

    problems: list[str] = []
    drivers = ((contract.get("graded_builds") or {}).get("drivers")) or []
    declared = [d.get("id") for d in drivers]
    for expected in {row[2] for row in DRIVER_RESOLUTION_CASES}:
        if expected not in declared:
            problems.append(
                f"the driver resolution table expects {expected!r}, which the "
                f"contract does not declare: {declared}"
            )
    if problems:
        return problems

    for label, files, expected in DRIVER_RESOLUTION_CASES:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for rel in files:
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("x", encoding="utf-8")
            try:
                got = resolve_driver(root, contract).get("id")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"resolving {label!r} raised "
                                f"{type(exc).__name__}: {exc}")
                continue
        if got != expected:
            problems.append(
                f"{label!r} resolves to {got!r}, expected {expected!r}"
            )

    # Both directions of the trap, stated as a property rather than a row: the two
    # drivers must not resolve the same tree, and every declared driver must be
    # reachable by some row.  A table that only ever exercised one driver would
    # pass while the other was unreachable.
    reached = {row[2] for row in DRIVER_RESOLUTION_CASES}
    unreached = [d for d in declared if d not in reached]
    if unreached:
        problems.append(
            f"no row in the driver resolution table resolves to {unreached}; "
            f"those drivers are declared but never exercised"
        )
    return problems


def driver_outputs(driver: dict) -> tuple[str, ...]:
    """The paths this driver's build is allowed to create in the tree."""
    if driver.get("toolchain") == "go":
        return GO_BUILD_OUTPUT_NAMES
    return BUILD_OUTPUT_NAMES


def driver_of(contract: dict, outcome=None) -> dict:
    """The driver the build ran, looked up by the id the build recorded.

    By id rather than by re-resolving, and this matters.  `resolve_driver` reads the
    tree; by the time the auditors run, the build has written into that tree, and a
    second resolution is a second chance to disagree with the first.  The id in
    `observed["driver"]` is what actually ran, so grading follows it.

    A missing record falls back to resolving, and that fallback is not defensive
    padding: `run_provenance_cases` is reachable with `outcome=None` when the build
    never produced one, and the checks still have to name an artefact to say it is
    absent.
    """
    recorded = ((getattr(outcome, "observed", None) or {}).get("driver") or {}).get("id")
    drivers = ((contract.get("graded_builds") or {}).get("drivers")) or []
    if recorded:
        for entry in drivers:
            if entry.get("id") == recorded:
                return entry
        raise RuntimeError(
            f"the build recorded driver {recorded!r}, which source-contract.json "
            f"does not declare: {[d.get('id') for d in drivers]}"
        )
    return drivers[-1] if drivers else {}


def remove_go_toolchain() -> dict:
    """Delete the Go toolchain from the container, and report what went.

    Called before a Zig build and after a Go one, so that no module which drives
    a submission's binary ever runs with a Go compiler present.  The return value
    is the evidence a scored case reads: "it was already gone" and "it was here
    and I removed it" are both fine, and "it is still here" is the failure.
    """
    removed = []
    for path in (GOROOT, GOMODCACHE, *GO_LEFTOVERS):
        if path.is_symlink() or path.exists():
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                # Go's module cache is written mode 0555, directories included, so
                # a plain rmtree fails on it -- and with ignore_errors that failure
                # is silent, which would leave a Go toolchain in the container while
                # the case that checks for one reported a clean removal.
                for sub in sorted(path.rglob("*"), reverse=True):
                    try:
                        if sub.is_dir() and not sub.is_symlink():
                            sub.chmod(0o755)
                    except OSError:
                        pass
                try:
                    path.chmod(0o755)
                except OSError:
                    pass
                shutil.rmtree(path, ignore_errors=True)
            removed.append(str(path))
    survivors = [str(p) for p in (GOROOT, GOMODCACHE, *GO_LEFTOVERS)
                 if p.exists() or p.is_symlink()]
    on_path = {}
    for tool in SHIM_TOOLS:
        found = shutil.which(tool, path=REAL_PATH)
        if found:
            on_path[tool] = found
    return {
        "removed": removed,
        "survivors": survivors,
        "on_real_path": on_path,
        "clean": not survivors and not on_path,
    }


@dataclass
class BuildOutcome:
    """What happened during one build of the submission."""

    build_dir: Path
    build: Result | None = None
    test_step: Result | None = None
    extra: dict[str, Result] = field(default_factory=dict)
    # What each probe observed while it ran, for the probes whose evidence does
    # not survive them.  The pre-build tree listing is the reason this exists: a
    # submission that committed a `.zig-cache/` can only be seen before the
    # discard, and after the build every submission has one.
    observed: dict[str, object] = field(default_factory=dict)
    shim_log: Path | None = None
    # What was removed before the graded build, by name.  The audit gates need
    # this because the build destroys the evidence they would look for.
    discarded: dict[str, str] = field(default_factory=dict)

    @property
    def built(self) -> bool:
        return self.build is not None and self.build.ok

    # -- crossing a process boundary ---------------------------------------
    # The behavioural suite builds once, in its own module, and twenty-one other
    # processes grade what that build produced.  Several of them need facts only the
    # build could observe -- the two rebuild durations, the paths it wrote into the
    # tree, what `zig version` printed -- and those exist nowhere on disk once the
    # build process has exited.  So the whole outcome is published as JSON and read
    # back, rather than each module re-deriving what it can and quietly failing the
    # cases about what it cannot.
    #
    # `observed` is JSON already: it is built from primitives on purpose, and
    # `to_json` asserts that rather than assuming it, because a probe that recorded
    # a Path in there would otherwise fail at write time in the build module and
    # take every other module down with it.

    def to_json(self) -> dict:
        payload = {
            "build_dir": str(self.build_dir),
            "built": self.built,
            "build": self.build.to_json() if self.build else None,
            "test_step": self.test_step.to_json() if self.test_step else None,
            "extra": {k: v.to_json() for k, v in sorted(self.extra.items())},
            "observed": self.observed,
            "shim_log": str(self.shim_log) if self.shim_log else None,
            "discarded": dict(sorted(self.discarded.items())),
        }
        json.dumps(payload["observed"])  # raises here, where the cause is visible
        return payload

    @classmethod
    def from_json(cls, raw: dict) -> "BuildOutcome":
        def result(value):
            return Result.from_json(value) if value else None

        return cls(
            build_dir=Path(raw["build_dir"]),
            build=result(raw.get("build")),
            test_step=result(raw.get("test_step")),
            extra={k: Result.from_json(v)
                   for k, v in (raw.get("extra") or {}).items()},
            observed=dict(raw.get("observed") or {}),
            shim_log=Path(raw["shim_log"]) if raw.get("shim_log") else None,
            discarded=dict(raw.get("discarded") or {}),
        )

    def summary(self) -> dict:
        payload = {
            "build_dir": str(self.build_dir),
            "built": self.built,
        }
        for name, result in (("build", self.build), ("test_step", self.test_step)):
            if result is not None:
                payload[name] = result.brief()
        if self.extra:
            payload["probes"] = {k: v.brief() for k, v in sorted(self.extra.items())}
        if self.discarded:
            payload["discarded"] = dict(sorted(self.discarded.items()))
        if self.observed:
            payload["observed"] = dict(sorted(self.observed.items()))
        return payload


def install_shim(shim_src: Path, shim_dir: Path, log: Log) -> Path:
    """Materialize the Go tripwire under every name it must intercept."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    target = shim_dir / "goshim.py"
    shutil.copy2(shim_src, target)
    target.chmod(0o755)
    for tool in SHIM_TOOLS:
        link = shim_dir / tool
        if link.exists() or link.is_symlink():
            link.unlink()
        # A symlink rather than a wrapper script, so argv[0] still names the tool
        # that was actually asked for -- `sh -c 'exec python3 goshim.py'` would
        # replace it with the shim's own name and the ledger would say nothing
        # useful about what the build tried to run.
        link.symlink_to("goshim.py")
    log.write(f"shim: installed {len(SHIM_TOOLS)} Go tripwires in {shim_dir}")
    return target


def shim_env(shim_dir: Path, shim_log: Path, *, home: Path, cache: Path,
             **overrides) -> dict:
    """The environment every graded build and probe run sees."""
    env = vlib.base_env(
        PATH=f"{shim_dir}:{REAL_PATH}",
        HOME=str(home),
        GOSHIM_LOG=str(shim_log),
        # Both cache variables, pointed at this run's scratch space.  Zig writes a
        # local cache next to build.zig unless told otherwise, and a cache the
        # verifier created inside the submission would be indistinguishable from
        # one the agent committed.
        ZIG_GLOBAL_CACHE_DIR=str(cache / "global"),
        ZIG_LOCAL_CACHE_DIR=str(cache / "local"),
        # Deterministic output: a submission that embeds a build timestamp or an
        # absolute path in a message would otherwise differ run to run.
        SOURCE_DATE_EPOCH="1700000000",
        LC_ALL="C.UTF-8",
        TZ="UTC",
        NO_COLOR="1",
    )
    env.update({k: str(v) for k, v in overrides.items() if v is not None})
    return env


class Builder:
    """Runs the published build for one submission."""

    def __init__(self, repo: Path, workspace: Path, shim_dir: Path, log: Log,
                 *, contract: dict) -> None:
        self.repo = repo
        self.workspace = workspace
        self.shim_dir = shim_dir
        self.log = log
        self.contract = contract
        self.driver = resolve_driver(repo, contract)
        self.outputs = driver_outputs(self.driver)
        # Read here rather than in each probe: the ops the standalone run asks are
        # the contract's published list, so a request set spelled in this file
        # could not drift out of agreement with the protocol the agent was given.
        self.protocol = contract.get("probe_protocol") or {}
        self.home = workspace / "buildhome"
        self.home.mkdir(parents=True, exist_ok=True)
        self.cache = workspace / "zigcache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.shim_log = workspace / "shim-build.jsonl"
        self.outcome = BuildOutcome(build_dir=repo, shim_log=self.shim_log)
        self.outcome.observed["driver"] = {
            "id": self.driver.get("id"),
            "toolchain": self.driver.get("toolchain"),
            "language": self.driver.get("language"),
            "detected_by": sorted(
                rel for rel in
                ((self.driver.get("detect") or {}).get("any_path_exists") or [])
                if (repo / rel).exists()
            ),
            "not_scored": list(self.driver.get(
                "provenance_checks_not_scored") or []),
            "artifact": self.binaries[0]["path"],
            "install": self._build_argv(),
            "test": self._test_argv(),
        }
        self.log.write(
            f"driver: {self.driver.get('id')} ({self.driver.get('language')}), "
            f"install `{' '.join(self._build_argv())}`, "
            f"artifact {self.binaries[0]['path']}"
        )

    @property
    def uses_go(self) -> bool:
        return self.driver.get("toolchain") == "go"

    @property
    def binaries(self) -> list[dict]:
        declared = self.driver.get("binaries") or []
        if not declared:
            raise RuntimeError(
                f"driver {self.driver.get('id')!r} declares no binaries; there is "
                f"nothing to grade"
            )
        return declared

    def env(self, **overrides) -> dict:
        """The environment for one graded command.

        On the Zig path this is the published one: the tripwire ahead of a PATH
        with no Go on it, and Zig's caches outside the tree.

        On the Go path the toolchain has to be reachable, so the tripwire is not
        installed and GOROOT/bin goes on PATH.  Everything else that makes the
        build hermetic stays: GOPROXY=off with a primed module cache, so nothing
        is fetched; CGO_ENABLED=0, so the binary is static and does not depend on
        this container's libc; and the build and module caches outside the tree,
        for the same reason Zig's are.
        """
        if not self.uses_go:
            return shim_env(self.shim_dir, self.shim_log, home=self.home,
                            cache=self.cache, **overrides)
        env = shim_env(self.shim_dir, self.shim_log, home=self.home,
                       cache=self.cache, **overrides)
        env["PATH"] = f"{GOROOT / 'bin'}:{REAL_PATH}"
        env.update({
            "GOROOT": str(GOROOT),
            "GOPATH": str(self.workspace / "gopath"),
            "GOCACHE": str(self.workspace / "gocache"),
            "GOMODCACHE": str(GOMODCACHE),
            "GOPROXY": "off",
            # readonly, not mod.  State A ships go-yaml v3.0.1's own go.mod:
            # quoted module paths and no `go` directive.  With `-mod=mod` the first
            # `go build` normalises the quotes and writes `go 1.23.4` into it --
            # measured -- which mutates the submission the harness then collects.
            # readonly refuses to write it and builds anyway, because the module
            # cache is primed: without a `go` directive the module graph is not
            # pruned, so check.v1's go.mod has to be present even to build a
            # package that does not import it.
            "GOFLAGS": "-mod=readonly",
            "CGO_ENABLED": "0",
            "GOTOOLCHAIN": "local",
        })
        return env

    # The argv the contract publishes, so a change to the contract cannot leave
    # this file grading a different command than the one the agent was promised.
    def _step_argv(self, step: str) -> list[str]:
        for entry in self.driver["build_steps"]:
            if entry["step"] == step:
                return list(entry["invoked_as"])
        raise RuntimeError(
            f"driver {self.driver.get('id')!r} declares no {step} build step"
        )

    def _build_argv(self) -> list[str]:
        return self._step_argv("install")

    def _test_argv(self) -> list[str]:
        return self._step_argv("test")

    def discard_artifacts(self) -> list[str]:
        """Remove anything the agent's container built.

        Rebuilding from source is the point.  A `zig-out/` from the agent's
        container was produced by a build this verifier did not watch: it could
        have had network access, and it certainly did not have the tripwire on its
        PATH.  It could also simply be a binary with no sources behind it.
        """
        removed = []
        for name in self.outputs:
            path = self.repo / name
            if path.is_symlink() or path.exists():
                # A symlink named `zig-out` pointing somewhere outside the
                # repository is worth recording rather than silently following.
                kind = "symlink" if path.is_symlink() else \
                    ("dir" if path.is_dir() else "file")
                if path.is_symlink() or path.is_file():
                    path.unlink()
                else:
                    shutil.rmtree(path, ignore_errors=True)
                removed.append(f"{name} ({kind})")
                self.outcome.discarded[name] = kind
        if removed:
            self.log.write(f"discarded pre-built artifacts: {', '.join(removed)}")
        return removed

    def _tree_listing(self) -> list[str]:
        """Every path in the repository, relative, sorted, excluding .git.

        Used twice: once before the build, to see what the submission actually
        contains, and once after, to see what the build added.  A submission is
        allowed to add zig-out/ and a cache; anything else it writes into its own
        tree during a build is a finding.
        """
        out = []
        for path in sorted(self.repo.rglob("*")):
            rel = path.relative_to(self.repo)
            if rel.parts and rel.parts[0] == ".git":
                continue
            out.append(str(rel) + ("/" if path.is_dir() and not path.is_symlink()
                                   else ""))
        return out

    def build(self) -> Result:
        argv = self._build_argv()
        result = vlib.run(argv, cwd=self.repo, env=self.env(),
                          timeout=BUILD_TIMEOUT, log=self.log, label="build")
        self.outcome.build = result
        binary = self.repo / self.binaries[0]["path"]
        self.outcome.observed["artifact"] = {
            "path": self.binaries[0]["path"],
            "exists": binary.is_file(),
            "bytes": binary.stat().st_size if binary.is_file() else 0,
            "mode": oct(binary.stat().st_mode & 0o7777) if binary.is_file() else None,
        }
        return result

    def test_step(self) -> Result:
        """The driver's declared test step must exist as a step.

        Not scored on what it reports.  The instruction says the step may run zero
        tests, so the only question is whether invoking it is an error of the "there
        is no such step" kind -- and both toolchains answer that distinctly, in
        their own spelling (`error: no step named 'test'`, `no packages to test`).
        Both are recorded here as text so the structural check can tell them from a
        test that ran and failed instead of reading a bare exit status; the patterns
        live in structure.py's `TEST_STEP_PATTERNS`.
        """
        result = vlib.run(self._test_argv(), cwd=self.repo, env=self.env(),
                          timeout=STEP_TIMEOUT, log=self.log, label="build-test")
        self.outcome.test_step = result
        return result

    def retire_go_toolchain(self, phase: str) -> dict:
        """Delete the Go toolchain and record that it is gone.

        Called once on either path -- before the build when the build is Zig,
        after it when the build is Go -- and once more from `finish`, so the
        record the scored case reads is of the state the behaviour modules
        actually ran in rather than of an intention stated here.
        """
        state = remove_go_toolchain()
        state["phase"] = phase
        history = self.outcome.observed.setdefault("go_toolchain", {})
        history[phase] = state
        history["retired_before_build"] = "before-build" in history
        if state["removed"]:
            self.log.write(
                f"go toolchain removed ({phase}): {', '.join(state['removed'])}")
        else:
            self.log.write(f"go toolchain already absent ({phase})")
        if not state["clean"]:
            self.log.write(
                f"WARNING: Go survived removal: dirs={state['survivors']} "
                f"path={state['on_real_path']}")
        return state

    def finish(self) -> dict:
        """After the build and its probes: no Go compiler, on either path."""
        return self.retire_go_toolchain("after-build")

    def run_all(self) -> BuildOutcome:
        self.log.section("build submission")
        # A Zig build never has a legitimate use for the Go toolchain, so it is
        # deleted before the build rather than merely kept off PATH.
        if not self.uses_go:
            self.retire_go_toolchain("before-build")
        # Before anything runs.  `zig build` creates .zig-cache/ and zig-out/, so
        # after the build every submission looks like it committed both; whether it
        # did can only be seen now.  The same listing is how a cache committed
        # under a different name is found later.
        before = self._tree_listing()
        self.outcome.observed["tree_before_build"] = {
            "entries": len(before),
            "top_level": sorted({p.split("/")[0] for p in before}),
        }
        self.outcome.observed["prebuilt_output"] = sorted(
            name for name in self.outputs if (self.repo / name).exists()
        )
        self.discard_artifacts()
        cleaned = self._tree_listing()
        result = self.build()
        after = self._tree_listing()
        # What the build added to the tree, minus what it is allowed to add.  This
        # is evidence for one structural case and one guard, and it cannot be
        # recomputed later: a second build would add the same paths again.
        allowed = tuple(f"{n}/" for n in self.outputs) + self.outputs
        added = [p for p in sorted(set(after) - set(cleaned))
                 if not p.startswith(allowed)]
        self.outcome.observed["build_added_paths"] = added[:200]
        if added:
            self.log.write(
                f"build wrote {len(added)} path(s) into the repository beyond "
                f"{', '.join(self.outputs)}: {', '.join(added[:8])}")
        if not result.ok:
            self.log.write("build failed; skipping the test step")
            return self.outcome
        self.test_step()
        return self.outcome

    # -- probes -----------------------------------------------------------
    # These are not part of the build; they are questions about it that only the
    # build can answer.

    def probe_rebuild(self) -> Result:
        """A second `zig build` must not relink the world.

        Zig's cache makes a no-op rebuild fast, so the measurement is time: a
        second build that takes as long as the first means the build graph is
        wrong -- usually a step with no declared output, which reruns always.  The
        comparison is left to the structural check; this records both durations.
        """
        result = vlib.run(self._build_argv(), cwd=self.repo, env=self.env(),
                          timeout=BUILD_TIMEOUT, log=self.log, label="rebuild")
        self.outcome.extra["rebuild"] = result
        first = self.outcome.build.duration if self.outcome.build else 0.0
        self.outcome.observed["rebuild"] = {
            "first_sec": round(first, 3),
            "second_sec": round(result.duration, 3),
            "ok": result.ok,
        }
        return result

    def probe_version(self) -> Result:
        """The compiler's own version, from the compiler the build actually ran.

        Read through the same PATH the build saw, not from a hard-coded location:
        the claim being checked is that the pinned toolchain built the submission,
        and a version read from somewhere else would not support it.

        Both drivers answer, under the tool each one used, and `toolchain-pinned`
        checks whichever is relevant.  Asking `zig version` on the Go path would
        record a real Zig version that had nothing to do with the binary.
        """
        argv = ["go", "version"] if self.uses_go else ["zig", "version"]
        label = "go-version" if self.uses_go else "zig-version"
        result = vlib.run(argv, cwd=self.repo, env=self.env(),
                          timeout=QUERY_TIMEOUT, log=self.log, label=label)
        self.outcome.extra[label.replace("-", "_")] = result
        self.outcome.observed["compiler_version"] = {
            "argv": argv,
            "text": result.text().strip()[:200],
        }
        if not self.uses_go:
            # Under the toolchain-agnostic label above and this one: `toolchain-pinned`
            # in structure.py and the contract's `state_b.zig_version` both read
            # `zig_version`, so the report keeps its shape whichever driver ran.
            self.outcome.observed["zig_version"] = result.text().strip()
        return result

    def probe_standalone(self, scratch: Path) -> Result:
        """The binary must still run with the source tree gone.

        The installed binary is copied out, and then the repository is *renamed
        away* for the duration of the run, so "the tree is gone" is the literal
        state of the filesystem rather than an inference from where the copy was
        started.  A probe that opens a file under `src/` at run time, or that is a
        shell script pointing back into the tree, cannot survive that; one that
        merely carries the path in its debug info is unaffected, which is the
        distinction this case is supposed to draw.

        Renaming rather than deleting, and restored in `finally`, because later
        modules walk the tree.  If the rename cannot be done -- a bind mount, a
        busy directory -- the run still happens with the tree present and the
        record says the stronger form did not apply, because a harness limitation
        must not read as a finding about the submission.

        Answers are compared against the same requests asked of the in-tree
        binary.  A binary that starts and prints an error is not "still working",
        and only a comparison catches that.
        """
        target = scratch / "standalone"
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        rel = self.binaries[0]["path"]
        src = self.repo / rel
        if not src.is_file():
            result = Result(argv=["<copy>", str(src)], cwd=str(scratch),
                            returncode=-1, stdout=b"",
                            stderr=b"the build produced no binary to copy",
                            duration=0.0)
            self.outcome.extra["standalone"] = result
            self.outcome.observed["standalone"] = {
                "ran": False,
                "reason": "the build produced no binary to copy",
            }
            return result
        dest = target / Path(rel).name
        shutil.copy2(src, dest)
        dest.chmod(0o755)

        # Every op the contract publishes, plus the handshake: enough that a probe
        # which needs a data file for one of them is asked for it.
        ops = list((self.protocol.get("operations") or []))
        handshake = self.protocol.get("handshake_op") or "hello"
        requests = [{"id": 1, "op": handshake, "source": ""}]
        for at, op in enumerate(ops, start=2):
            requests.append({"id": at, "op": op,
                             "source": "a: [1, 2]\nb: &x {c: d}\ne: *x\n"})
        stdin_data = ("\n".join(json.dumps(r, separators=(",", ":"))
                                for r in requests) + "\n").encode()

        # The in-tree run first, as the thing to compare against.  A bare
        # environment on both sides: a binary that only answers with the build's
        # PATH set is not a deliverable, and using the build env would hide it.
        bare = vlib.base_env(PATH="/usr/local/bin:/usr/bin:/bin")
        in_tree = vlib.run([str(src)], cwd=self.repo, env=bare,
                           timeout=QUERY_TIMEOUT, log=self.log,
                           label="standalone-in-tree", stdin_data=stdin_data)

        hidden = self.repo.parent / (self.repo.name + ".hidden-for-standalone")
        moved = False
        try:
            if not hidden.exists():
                try:
                    self.repo.rename(hidden)
                    moved = True
                except OSError as exc:
                    self.log.write(
                        f"standalone: could not rename the tree away ({exc}); "
                        f"running the copy with the tree still present")
            result = vlib.run([str(dest)], cwd=target, env=bare,
                              timeout=QUERY_TIMEOUT, log=self.log,
                              label="standalone", stdin_data=stdin_data)
        finally:
            if moved:
                try:
                    hidden.rename(self.repo)
                except OSError as exc:  # pragma: no cover - would break later modules
                    self.log.write(
                        f"FATAL: the repository could not be restored from "
                        f"{hidden}: {exc}")
                    raise

        self.outcome.extra["standalone"] = result
        self.outcome.extra["standalone_in_tree"] = in_tree
        same = result.stdout == in_tree.stdout
        self.outcome.observed["standalone"] = {
            "ran": True,
            "dir": str(target),
            "tree_renamed_away": moved,
            "requests": len(requests),
            "stdout": result.stdout[:2000].decode("utf-8", "replace"),
            "returncode": result.returncode,
            "in_tree_returncode": in_tree.returncode,
            "answers_match_in_tree": same,
            "in_tree_stdout": (
                in_tree.stdout[:2000].decode("utf-8", "replace") if not same else None
            ),
        }
        return result

    def probe_cache_outside_tree(self) -> Result:
        """A third build with the cache variables unset.

        This is the one probe that runs the build *differently* from the graded
        one, and it answers a question the graded build cannot: whether the build
        script hard-codes a cache location inside the repository.  With the
        variables unset each toolchain has a default, and the default is allowed --
        so what this looks for is a cache appearing somewhere the toolchain would
        not have put one.  On the Zig path that default is `.zig-cache/` next to
        build.zig, which is one of the driver's own declared outputs.
        """
        env = self.env()
        env.pop("ZIG_GLOBAL_CACHE_DIR", None)
        env.pop("ZIG_LOCAL_CACHE_DIR", None)
        # The same question for the Go driver: with GOCACHE unset the default is
        # under HOME, which is outside the tree, so a cache appearing inside it is
        # the build's own doing.  GOMODCACHE stays -- unsetting it would send the
        # module lookup to a directory with nothing in it and fail the build for a
        # reason that is not the one being asked about.
        if self.uses_go:
            env.pop("GOCACHE", None)
            env.pop("GOPATH", None)
        env["HOME"] = str(self.home)
        before = set(self._tree_listing())
        result = vlib.run(self._build_argv(), cwd=self.repo, env=env,
                          timeout=BUILD_TIMEOUT, log=self.log,
                          label="build-default-cache")
        after = set(self._tree_listing())
        allowed = tuple(f"{n}/" for n in self.outputs) + self.outputs
        stray = sorted(p for p in (after - before) if not p.startswith(allowed))
        self.outcome.extra["default_cache_build"] = result
        self.outcome.observed["default_cache_build"] = {
            "ok": result.ok,
            "stray_paths": stray[:100],
        }
        return result


def read_shim_ledger(path: Path) -> list[dict]:
    """Every Go-toolchain invocation the build attempted.

    A malformed line is kept as a raw record rather than dropped: the ledger is
    evidence, and "something wrote garbage here" is itself worth reporting.
    """
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"tool": "<unparseable>", "raw": line[:400]})
    return out
