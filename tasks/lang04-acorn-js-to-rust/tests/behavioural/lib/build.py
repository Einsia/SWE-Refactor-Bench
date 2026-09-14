#!/usr/bin/env python3
"""Builds the submission the way the instruction says it will be built.

`make build`, then `make install PREFIX=...`, offline, with no JavaScript
interpreter reachable from anywhere in the build.  The argv is the one published
in source-contract.json, verbatim -- nothing is special-cased for grading.

Two things about the environment are deliberate and run against the submission
rather than for it:

* Cargo is put in offline mode by configuration, not by hoping the container has
  no network.  A submission that added a crates.io dependency fails here with
  cargo's own message, which is more useful than a network error at a random
  point in the graph.
* Every JavaScript runtime on PATH is replaced by a tripwire.  The build has no
  legitimate use for one, so any invocation is both refused and recorded.

`target/` is discarded before building.  Whatever the agent's container left
there was produced by a build this verifier did not supervise, possibly with
network access and certainly without the tripwire.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import vlib
from vlib import Log, Result

BUILD_TIMEOUT = 5400.0
INSTALL_TIMEOUT = 600.0
METADATA_TIMEOUT = 300.0

# Every name a JavaScript runtime could plausibly arrive through, including the
# package managers, because `npm run` and `npx` are how a JS build step is
# usually spelled.  `deno`, `bun` and `qjs` are here because "no node" is not
# the same claim as "no interpreter".
SHIM_TOOLS = (
    "node", "nodejs", "npm", "npx", "yarn", "pnpm", "corepack",
    "deno", "bun", "bunx", "qjs", "qjsc", "d8", "jsc", "js",
    "rhino", "spidermonkey", "hermes", "graaljs", "ts-node", "tsc",
)

# The real toolchain.  The tripwire directory is prepended to this, never
# substituted for it: the build needs cargo, rustc, cc (as rustc's linker
# driver), make, ar and ld.
REAL_PATH = "/usr/local/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# The binaries a release build is expected to leave in target/release, used only
# to describe what `make clean` did or did not remove.  What must be *installed*
# comes from the contract's inventory; these are the intermediate artifacts.
RELEASE_BINARIES = ("acorn", "acorn-probe")

#: The language a tree is written in, which decides which builder runs it.
#:
#: Read off the tree rather than passed in as a flag.  A flag would be a second
#: thing to keep in step with reality, and the interesting case -- the operator
#: grading State A to find out whether the corpus is answerable at all -- is
#: exactly the case where nobody remembers to pass it.
LANG_RUST = "rust"
LANG_JS = "javascript"


@dataclass
class BuildOutcome:
    """What happened during one build of the submission."""

    build_dir: Path
    prefix: Path
    build: Result | None = None
    install: Result | None = None
    metadata: Result | None = None
    #: `cargo metadata`'s document, carried as JSON because `brief()` clips the
    #: result beside it to log length.
    metadata_json: dict | None = None
    extra: dict[str, Result] = field(default_factory=dict)
    # What each probe observed while it ran, for the probes whose evidence does
    # not survive them.  `make clean` is the reason this exists: whether it
    # removed the release binaries can only be seen between that probe and the
    # next build, so the answer is recorded here rather than left for a later
    # phase to read off a filesystem that has moved on.
    observed: dict[str, object] = field(default_factory=dict)
    shim_log: Path | None = None
    # What was removed before the graded build, by name.  The audit gates
    # need this because the build destroys the evidence they would look for: a
    # submission that shipped `node_modules/` or `vendor/` has already had them
    # deleted by the time the gates walk the tree.
    discarded: dict[str, str] = field(default_factory=dict)
    #: Which builder produced this outcome -- LANG_RUST or LANG_JS.  Set from
    #: `detect_language` and carried across the process boundary, because the
    #: modules that read this outcome have to know which questions its fields can
    #: answer: `build` means `make build` under one and a dist bridge plus a load
    #: check under the other, and the cases that read an ELF header or a crate
    #: graph can only be asked of the first.  Read by
    #: `structure.JS_STRUCT_SKIP_GROUPS`, which is the only consumer.
    language: str = "rust"

    @property
    def built(self) -> bool:
        return self.build is not None and self.build.ok

    @property
    def installed(self) -> bool:
        return self.install is not None and self.install.ok

    def summary(self) -> dict:
        payload = {
            "build_dir": str(self.build_dir),
            "prefix": str(self.prefix),
            "built": self.built,
            "installed": self.installed,
            # Recorded because the ledger outlives this process: the `shim-clean`
            # gate runs in a later module and reads the file this names. Omitting
            # it would leave that gate reading an absent ledger, which looks
            # exactly like a build that asked for no interpreter.
            "shim_log": str(self.shim_log) if self.shim_log else None,
            "language": self.language,
        }
        for name, result in (("build", self.build), ("install", self.install),
                             ("metadata", self.metadata)):
            if result is not None:
                payload[name] = result.brief()
        meta_json = self.metadata_json
        if meta_json is None and self.metadata is not None and self.metadata.ok:
            try:
                meta_json = json.loads(self.metadata.stdout)
            except ValueError:
                meta_json = None
        if isinstance(meta_json, dict):
            payload["metadata_json"] = meta_json
        if self.extra:
            payload["probes"] = {k: v.brief() for k, v in sorted(self.extra.items())}
        if self.discarded:
            payload["discarded"] = dict(sorted(self.discarded.items()))
        if self.observed:
            payload["observed"] = dict(sorted(self.observed.items()))
        return payload

    @classmethod
    def from_summary(cls, payload: dict, workspace: Path) -> "BuildOutcome":
        """Rebuild what `summary()` wrote, for a later module in the same suite.

        The behavioural suite runs one module per process against a shared work
        directory, so the build's outcome crosses a process boundary as JSON.
        Everything the modules after `build` actually read survives that trip:
        the prefix, whether the build and install succeeded, each probe's exit
        status, what was discarded before the build, and what `make clean` was
        observed to remove.

        What does not survive is the full output of each command -- `brief()`
        clips it -- so a rehydrated result is fit for `ok`, `returncode` and
        `tail()` and not for comparing bytes.  Nothing downstream compares bytes
        from a build command; the differential compares the probe's answers,
        which are produced live.
        """
        outcome = cls(
            build_dir=Path(payload.get("build_dir") or workspace / "repo"),
            prefix=Path(payload.get("prefix") or workspace / "install"),
            shim_log=Path(payload["shim_log"]) if payload.get("shim_log") else None,
            discarded=dict(payload.get("discarded") or {}),
            observed=dict(payload.get("observed") or {}),
            # Defaulted to rust rather than re-detected from the tree.  A build
            # state written by an older image carries no language, and the tree it
            # describes is whatever it was: guessing again here could disagree with
            # the builder that actually ran, and the skip table would then excuse
            # cases that were asked and answered.
            language=str(payload.get("language") or LANG_RUST),
        )
        for name in ("build", "install", "metadata"):
            if isinstance(payload.get(name), dict):
                setattr(outcome, name, Result.from_brief(payload[name]))
        if isinstance(payload.get("metadata_json"), dict):
            outcome.metadata_json = payload["metadata_json"]
        for name, brief in sorted((payload.get("probes") or {}).items()):
            if isinstance(brief, dict):
                outcome.extra[name] = Result.from_brief(brief)
        return outcome


def install_shim(shim_src: Path, shim_dir: Path, log: Log) -> Path:
    """Materialize the interpreter tripwire under every name it must intercept."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    target = shim_dir / "jsshim.py"
    shutil.copy2(shim_src, target)
    target.chmod(0o755)
    for tool in SHIM_TOOLS:
        link = shim_dir / tool
        if link.exists() or link.is_symlink():
            link.unlink()
        # A symlink rather than a wrapper script, so argv[0] still names the tool
        # that was actually asked for -- `sh -c 'exec python3 jsshim.py'` would
        # replace it with the shim's own name and the ledger would say nothing
        # useful about what the build tried to run.
        link.symlink_to("jsshim.py")
    log.write(f"shim: installed {len(SHIM_TOOLS)} interpreter tripwires in {shim_dir}")
    return target


def shim_env(shim_dir: Path, shim_log: Path, *, home: Path, **overrides) -> dict:
    """The environment every graded build and probe run sees."""
    env = vlib.base_env(
        PATH=f"{shim_dir}:{REAL_PATH}",
        HOME=str(home),
        JSSHIM_LOG=str(shim_log),
        # Offline is set three ways because cargo reads all three and a
        # submission's own config.toml could override one of them.
        CARGO_NET_OFFLINE="true",
        CARGO_HOME=str(home / ".cargo"),
        CARGO_TERM_COLOR="never",
        RUSTUP_TOOLCHAIN="1.90.0",
        # Deterministic output: a submission that embeds a build timestamp or an
        # absolute path in a message would otherwise differ run to run.
        SOURCE_DATE_EPOCH="1700000000",
        LC_ALL="C.UTF-8",
        TZ="UTC",
    )
    env.update({k: str(v) for k, v in overrides.items() if v is not None})
    return env


def write_cargo_config(home: Path, log: Log) -> Path:
    """A cargo config that forbids the network and pins the target dir.

    `offline = true` here rather than only in the environment: the submission's
    own `.cargo/config.toml` in the repository takes precedence over environment
    defaults for some keys, and this one is read from CARGO_HOME.
    """
    cargo_home = home / ".cargo"
    cargo_home.mkdir(parents=True, exist_ok=True)
    config = cargo_home / "config.toml"
    config.write_text(
        "[net]\n"
        "offline = true\n"
        "git-fetch-with-cli = false\n"
        "[build]\n"
        "incremental = false\n"
        "[term]\n"
        "quiet = false\n",
        encoding="utf-8",
    )
    log.write(f"cargo: offline config at {config}")
    return config


def port_evidence(repo: Path) -> str:
    """The first sign this tree is being ported, or `""` for none at all.

    Deliberately generous about what counts, because of what the answer is used
    for: `JsBuilder` is not the submission's build.  acorn ships no Makefile, so
    there is no `make build` for a pre-migration tree to run, and the JavaScript
    path substitutes a builder the task author wrote to prove the corpus is
    answerable at all.  Routing a submission onto it measures that builder's
    artifacts rather than the submission's, hands it the ten-of-seventeen
    structural split that was measured on State A, and skips all six provenance
    gates -- so the mistake to avoid is calling a port pre-migration, and any
    evidence at all is enough to avoid it.

    Not a filename check on `Cargo.toml` alone: a manifest under `acorn/` with no
    workspace root is a non-conforming port, and grading it as State A reports
    "behaviour preserved" about a build that never ran.  A single `.rs` file
    settles it.
    """
    if (repo / "Cargo.toml").is_file():
        return "Cargo.toml"
    for name in ("Cargo.lock", "Makefile", "GNUmakefile", "makefile",
                 "rust-toolchain.toml", "rust-toolchain"):
        if (repo / name).exists():
            return name
    # `target/` is a build product this verifier discards and rebuilds, and
    # `node_modules/` is State A's own dependency tree -- neither says anything
    # about what was written.  Pruned rather than filtered so a warm cargo cache
    # does not make this walk expensive.
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "target", "node_modules")]
        for filename in filenames:
            if filename.endswith(".rs"):
                return str(Path(dirpath, filename).relative_to(repo))
    return ""


def detect_language(repo: Path) -> str:
    """Which of the two trees is this?

    Any evidence of a port decides, and decides first.  A submission is graded on
    its Rust whatever else it also ships: a port that left acorn's `package.json`
    in place beside its own sources is still a port, and must not be routed onto
    the pre-migration path where seven structural checks are skipped and all six
    provenance gates go unasked.  That ordering is what stops this function ever
    lowering a submission's score -- and, since the JavaScript path is the one
    that takes State A to full marks, ever raising it either.

    The failure this guards against is concrete: a tree with sixteen `.rs` files
    under `acorn/src/` and no workspace manifest, graded `javascript, built by
    JsBuilder` and paid in full on JsBuilder's artifacts, having never run its own
    build.  A routing rule that reads only the repo root does exactly that, while
    `JsBuilder`'s own docstring promises "a Rust submission never reaches this
    class".

    The JavaScript answer therefore requires no port evidence anywhere *and* the
    presence of acorn's own layout, so an empty directory or a tree that is
    neither gets graded as a Rust submission that does not build -- which is the
    correct verdict for it.
    """
    if port_evidence(repo):
        return LANG_RUST
    if (repo / "package.json").is_file() and (repo / "acorn/src/index.js").is_file():
        return LANG_JS
    return LANG_RUST


def find_node() -> Path | None:
    """The image's own `node`, by absolute path, or None if it is not there.

    Resolved off `REAL_PATH` rather than the caller's PATH, because every graded
    subprocess runs with the tripwire directory prepended and would find the shim
    instead.  Returns None rather than raising: whether an absent `node` is a
    grading result or an image defect depends on the caller, and both callers say
    so themselves.
    """
    found = shutil.which("node", path=REAL_PATH)
    return Path(found).resolve() if found else None


def make_builder(repo: Path, workspace: Path, shim_dir: Path, log: Log,
                 *, contract: dict, assets: Path, language: str = "") -> "Builder":
    """The builder for whatever this tree turns out to be.

    `language` overrides the detection, and exists for one caller: a module after
    `build` rehydrates the recorded outcome and passes the language that outcome
    was produced under.  Re-detecting there would be a second opinion about a tree
    that has since been built, and if the two ever disagreed the skip table would
    excuse cases that had in fact been asked.
    """
    language = language or detect_language(repo)
    cls = JsBuilder if language == LANG_JS else Builder
    log.write(f"tree at {repo}: {language}, built by {cls.__name__}")
    builder = cls(repo, workspace, shim_dir, log, contract=contract, assets=assets)
    builder.outcome.language = language
    return builder


class Builder:
    """Runs the published build for one submission."""

    def __init__(self, repo: Path, workspace: Path, shim_dir: Path, log: Log,
                 *, contract: dict, assets: Path | None = None) -> None:
        self.repo = repo
        self.workspace = workspace
        self.shim_dir = shim_dir
        self.log = log
        self.contract = contract
        # The frozen assets root.  Unused by this builder and required by
        # `JsBuilder`, which needs the recorded probe protocol from it; accepted
        # here so the two are constructed identically and `make_builder` needs no
        # per-class argument list.
        self.assets = assets
        self.home = workspace / "buildhome"
        self.home.mkdir(parents=True, exist_ok=True)
        self.shim_log = workspace / "shim-build.jsonl"
        self.prefix = workspace / "install"
        self.outcome = BuildOutcome(
            build_dir=repo, prefix=self.prefix, shim_log=self.shim_log
        )

    def env(self, **overrides) -> dict:
        return shim_env(self.shim_dir, self.shim_log, home=self.home, **overrides)

    def discard_artifacts(self) -> list[str]:
        """Remove anything the agent's container built.

        Rebuilding from source is the point.  A `target/` from the agent's
        container was produced by a build this verifier did not watch: it could
        have had network access, and it certainly did not have the tripwire on
        its PATH.
        """
        removed = []
        for name in ("target", "node_modules", "dist", ".cargo", "vendor"):
            path = self.repo / name
            if path.is_symlink() or path.exists():
                # A symlink named `target` pointing somewhere outside the
                # repository is worth recording rather than silently following.
                kind = "symlink" if path.is_symlink() else "dir"
                if path.is_symlink():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink()
                removed.append(f"{name} ({kind})")
                self.outcome.discarded[name] = kind
        if removed:
            self.log.write(f"discarded pre-built artifacts: {', '.join(removed)}")
        return removed

    def build(self) -> Result:
        argv = list(self.contract["build_contract"]["build"])
        result = vlib.run(argv, cwd=self.repo, env=self.env(),
                          timeout=BUILD_TIMEOUT, log=self.log, label="build")
        self.outcome.build = result
        # What landed in target/release, recorded now.  `make clean` runs later
        # and removes it, so this is the only moment the answer exists.
        release = self.repo / "target/release"
        self.outcome.observed["release_artifacts"] = {
            name: (release / name).is_file() for name in RELEASE_BINARIES
        }
        return result

    def install(self) -> Result:
        if self.prefix.exists():
            shutil.rmtree(self.prefix, ignore_errors=True)
        self.prefix.mkdir(parents=True, exist_ok=True)
        argv = [a.replace("<prefix>", str(self.prefix))
                for a in self.contract["build_contract"]["install"]]
        result = vlib.run(argv, cwd=self.repo, env=self.env(),
                          timeout=INSTALL_TIMEOUT, log=self.log, label="install")
        self.outcome.install = result
        return result

    def metadata(self) -> Result:
        """`cargo metadata`, which is how the crate versions are read.

        Parsing Cargo.toml by hand would miss a workspace that sets versions
        through `workspace.package`, and the instruction pins the versions per
        crate, so the resolved values are what matter.
        """
        result = vlib.run(
            ["cargo", "metadata", "--format-version", "1", "--no-deps",
             "--offline", "--locked"],
            cwd=self.repo, env=self.env(), timeout=METADATA_TIMEOUT,
            log=self.log, label="cargo-metadata",
        )
        if not result.ok:
            # `--locked` fails when Cargo.lock is absent or stale.  That is worth
            # knowing, but it must not cost the submission its version check, so
            # a second attempt without it decides the outcome.
            self.log.write("cargo metadata --locked failed; retrying unlocked")
            result = vlib.run(
                ["cargo", "metadata", "--format-version", "1", "--no-deps",
                 "--offline"],
                cwd=self.repo, env=self.env(), timeout=METADATA_TIMEOUT,
                log=self.log, label="cargo-metadata-unlocked",
            )
        self.outcome.metadata = result
        return result

    def run_all(self) -> BuildOutcome:
        self.log.section("build submission")
        write_cargo_config(self.home, self.log)
        # Before anything runs: cargo writes Cargo.lock during the build, so
        # after the build every submission looks like it committed one.  Whether
        # it actually did can only be seen now.
        lock = self.repo / "Cargo.lock"
        self.outcome.observed["lockfile"] = {
            "committed": lock.is_file(),
            "sha256": vlib.sha256_file(lock) if lock.is_file() else None,
        }
        self.discard_artifacts()
        result = self.build()
        if not result.ok:
            self.log.write("build failed; skipping install")
            return self.outcome
        self.install()
        self.metadata()
        return self.outcome

    # -- probes -----------------------------------------------------------
    # These are not part of the build; they are questions about it that only the
    # build can answer.

    def probe_rebuild(self) -> Result:
        """A second `make build` must be a no-op, not a rebuild.

        A Makefile that reruns cargo unconditionally still works; one that
        rebuilds the world because its dependencies are wrong is a defect a
        downstream packager would hit immediately.
        """
        result = vlib.run(list(self.contract["build_contract"]["build"]),
                          cwd=self.repo, env=self.env(), timeout=BUILD_TIMEOUT,
                          log=self.log, label="rebuild")
        self.outcome.extra["rebuild"] = result
        return result

    def probe_clean(self) -> Result:
        """`make clean`, if the Makefile has one, must not fail.

        What it removed is recorded here, not left for the structural phase to
        read later: any build that runs after this one puts the binaries back,
        and then the same filesystem gives the opposite answer.
        """
        release = self.repo / "target/release"
        before = sorted(
            name for name in RELEASE_BINARIES if (release / name).exists()
        )
        result = vlib.run(["make", "clean"], cwd=self.repo, env=self.env(),
                          timeout=INSTALL_TIMEOUT, log=self.log,
                          label="clean")
        after = sorted(
            name for name in RELEASE_BINARIES if (release / name).exists()
        )
        self.outcome.extra["clean"] = result
        self.outcome.observed["clean"] = {
            "binaries_before": before,
            "binaries_after": after,
            "target_exists_after": (self.repo / "target").is_dir(),
        }
        return result

    def probe_reinstall(self, scratch: Path) -> Result:
        """Installing to a second prefix must work and must be identical.

        A prefix baked in at build time rather than read at install time is the
        usual way this goes wrong, and it is invisible when only one prefix is
        ever used.
        """
        target = scratch / "install2"
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        argv = [a.replace("<prefix>", str(target))
                for a in self.contract["build_contract"]["install"]]
        result = vlib.run(argv, cwd=self.repo, env=self.env(),
                          timeout=INSTALL_TIMEOUT, log=self.log,
                          label="reinstall")
        self.outcome.extra["reinstall"] = result
        # The path and the resulting tree, so the structural check compares what
        # this probe actually produced instead of guessing where it went.
        self.outcome.observed["reinstall"] = {
            "prefix": str(target),
            "files": sorted(
                str(p.relative_to(target))
                for p in target.rglob("*") if p.is_file() and not p.is_symlink()
            ),
        }
        return result

    def probe_offline_fetch(self) -> Result:
        """`cargo fetch --offline` succeeds only if every dependency is vendored.

        The instruction allows std, core and alloc and nothing else.  This is the
        cheapest way to ask cargo itself whether that held, and its answer is
        more precise than reading Cargo.toml.
        """
        result = vlib.run(["cargo", "fetch", "--offline", "--locked"],
                          cwd=self.repo, env=self.env(), timeout=METADATA_TIMEOUT,
                          log=self.log, label="cargo-fetch")
        if not result.ok:
            # `--locked` fails when Cargo.lock is missing or stale, which is a
            # different fault from needing the network and is graded separately.
            # Retrying without it keeps this probe measuring the one thing it is
            # for, so a missing lockfile costs its own case and not two.
            self.log.write("cargo fetch --locked failed; retrying unlocked")
            relaxed = vlib.run(["cargo", "fetch", "--offline"],
                               cwd=self.repo, env=self.env(),
                               timeout=METADATA_TIMEOUT, log=self.log,
                               label="cargo-fetch-unlocked")
            self.outcome.extra["offline_fetch_locked"] = result
            result = relaxed
        self.outcome.extra["offline_fetch"] = result
        return result


    def probe_install_from_clean(self, scratch: Path) -> Result:
        """`make install` on a tree that has not been built.

        Run after `make clean`, so the artifacts really are absent.  A Makefile
        whose `install` assumes `target/release` is already populated works for
        whoever types the two commands in order and fails for every packaging
        script that does not -- and the instruction promises only that
        `make install PREFIX=<dir>` produces the inventory.
        """
        target = scratch / "install3"
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        argv = [a.replace("<prefix>", str(target))
                for a in self.contract["build_contract"]["install"]]
        result = vlib.run(argv, cwd=self.repo, env=self.env(),
                          timeout=BUILD_TIMEOUT, log=self.log,
                          label="install-from-clean")
        self.outcome.extra["install_from_clean"] = result
        self.outcome.observed["install_from_clean"] = {
            "prefix": str(target),
            "files": sorted(
                str(p.relative_to(target))
                for p in target.rglob("*") if p.is_file() and not p.is_symlink()
            ),
        }
        return result


#: What upstream's rollup writes into each package's `dist/`, and what this
#: builder writes there instead: a re-export bridge over the same ESM source.
#:
#: (package, artifact, contents).  `require(esm)` has been unflagged since Node
#: 22.12 and the image pins 22.20, so a one-line CommonJS bridge over
#: `src/index.js` is a working UMD-position artifact.  It is not byte-identical to
#: what rollup emits -- it is not transpiled and not bundled -- and it does not
#: need to be: what consumes these is `require()`, and what the corpus grades is
#: the behaviour reached through it.
JS_DIST_BRIDGES = (
    ("acorn", "dist/acorn.js", 'module.exports = require("../src/index.js")\n'),
    ("acorn", "dist/acorn.mjs", 'export * from "../src/index.js"\n'),
    ("acorn", "dist/bin.js", 'require("../src/bin/acorn.js")\n'),
    ("acorn-loose", "dist/acorn-loose.js",
     'module.exports = require("../src/index.js")\n'),
    ("acorn-loose", "dist/acorn-loose.mjs", 'export * from "../src/index.js"\n'),
    ("acorn-walk", "dist/walk.js", 'module.exports = require("../src/index.js")\n'),
    ("acorn-walk", "dist/walk.mjs", 'export * from "../src/index.js"\n'),
)

#: The three workspace packages, which is also what `node_modules` must link so
#: that acorn-loose's bare `import ... from "acorn"` resolves.
JS_PACKAGES = ("acorn", "acorn-loose", "acorn-walk")


class JsBuilder(Builder):
    """Builds and installs acorn's own npm tree -- State A, before the migration.

    WHY THIS EXISTS

    A repo-rewrite task preserves behaviour, so the tree the corpus was frozen
    from has to be able to answer the corpus.  Without a builder that can run it,
    it could not: `make build` exits 2 on a tree with no Makefile, and thirteen
    modules then read an install prefix that was never written.  Every behavioural
    case came back `fail -- the submission installed no bin/acorn-probe`, which
    reported the corpus as unanswerable when what was missing was a builder.

    WHAT IT IS ALLOWED TO DO

    Produce the artifacts the readers downstream of it need, from the submitted
    tree's own source, using tools the image has.  Not: substitute the harness's
    pinned copy of acorn for the tree (that would grade `/opt/assets` instead of
    what was handed in), and not run the contract's `make` argv, which State A
    genuinely does not have -- the analogue is run, and the checks that can only
    be answered by the Rust artefact are skipped by name in
    `structure.JS_STRUCT_SKIP_GROUPS` rather than answered from absence.

    WHY IT CANNOT LOOSEN GRADING

    `detect_language` routes here only for a tree carrying no evidence of a port
    at all -- no manifest, no lockfile, no root Makefile, not one `.rs` file
    anywhere -- and every behavioural case is still measured through
    `prefix/bin/acorn-probe`, the same path a submission's probe is measured
    through, byte for byte against the same frozen expectations.  A Rust
    submission never reaches this class.

    That last sentence is only true because the routing reads more than the repo
    root: a submission with sixteen `.rs` files under `acorn/src/` and no
    workspace manifest would reach this class and take full marks on artifacts this
    builder produced, which is why the rule is the absence of any evidence rather
    than the absence of one filename.  `port_evidence` is what makes the sentence
    true, and `_lang_routing_problems` fails the build if it stops being.
    """

    #: The build runs `node`, so it cannot run behind the tripwire.  This is the
    #: one place in the suite where that is true, and it is true for the same
    #: reason the tripwire exists: the shim's job is to fail any build that
    #: reaches for a JavaScript interpreter, and this build *is* a JavaScript
    #: interpreter's.  Running it behind the shim would measure the shim.
    #:
    #: The consequence is recorded rather than hidden, in `JS_PROV_SKIP`: each of
    #: the six artifact-backed guards is mapped to the reason this path cannot
    #: answer it, because each reads a Rust artefact or a ledger the build does
    #: not produce.  `shim-clean` is the sharpest -- it would otherwise report
    #: "the build invoked no JavaScript interpreter" about a build whose every
    #: step was `node`.
    uses_shim = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        node = find_node()
        if node is None:
            raise SystemExit(
                "this tree is JavaScript and there is no `node` on the verifier's "
                "own PATH, so it cannot be run at all. That is an image defect: "
                "the Dockerfile asserts node survives into the grading stage."
            )
        self.node = node

    def env(self, **overrides) -> dict:
        base = vlib.base_env(PATH=REAL_PATH, HOME=str(self.home))
        base.update({k: v for k, v in overrides.items() if v is not None})
        return base

    def discard_artifacts(self) -> list[str]:
        """Same contract as the Rust builder: nothing arrives pre-built.

        `node_modules` and `dist` are on the discard list the contract publishes,
        and they are exactly what this builder produces -- so removing them here
        is what makes the build below a real build rather than a check that the
        agent's container left the right files behind.
        """
        return super().discard_artifacts()

    # -- the build ---------------------------------------------------------

    def build(self) -> Result:
        """The analogue of `make build`: produce `dist/` and prove it loads.

        Upstream builds these with rollup and buble, which the grading image
        deletes (`npm has no role at grading time`).  What replaces them is a
        re-export bridge per artifact -- see JS_DIST_BRIDGES -- and the build is
        only reported as succeeding if every package then loads through the same
        `require()` that `reference.js` uses, at the version the contract pins.

        That last part is what makes this a build and not a file copy: it can
        fail, and it fails for the reasons a build fails -- a syntax error in the
        source, a missing module, a version that does not match.
        """
        self._link_workspaces()
        written = self._write_bridges()
        result = self._verify_loads()
        self.outcome.build = result
        self.outcome.observed["js_build"] = {
            "bridges_written": written,
            "packages_linked": list(JS_PACKAGES),
        }
        # `release_artifacts` is deliberately not written.  The Rust builder
        # records what landed in target/release; there is no target/release here,
        # and writing an empty inventory would be a claim that the build looked and
        # found nothing.  The case that reads it is skipped by name instead.
        return result

    def _link_workspaces(self) -> None:
        """`node_modules/<pkg>` -> `../<pkg>`, which is what npm workspaces do.

        acorn-loose's source imports the bare specifier `"acorn"` five times, so
        without these links it does not load at all.  Relative symlinks, so the
        tree stays valid when a later probe copies or moves it.
        """
        node_modules = self.repo / "node_modules"
        node_modules.mkdir(parents=True, exist_ok=True)
        for name in JS_PACKAGES:
            link = node_modules / name
            if link.is_symlink() or link.exists():
                if link.is_symlink() or link.is_file():
                    link.unlink()
                else:
                    shutil.rmtree(link, ignore_errors=True)
            os.symlink(f"../{name}", link)

    def _write_bridges(self) -> list[str]:
        written = []
        for package, relpath, contents in JS_DIST_BRIDGES:
            target = self.repo / package / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")
            written.append(f"{package}/{relpath}")
        # The .d.ts files rollup copies alongside them.  Not graded by anything
        # here, written because the artifact set is the thing being reproduced and
        # a consumer that reads types would otherwise find half a package.
        for package, src, dst in (
            ("acorn", "src/acorn.d.ts", "dist/acorn.d.ts"),
            ("acorn-loose", "src/acorn-loose.d.ts", "dist/acorn-loose.d.ts"),
            ("acorn-walk", "src/walk.d.ts", "dist/walk.d.ts"),
        ):
            source = self.repo / package / src
            if source.is_file():
                for suffix in (dst, dst.replace(".d.ts", ".d.mts")):
                    shutil.copy2(source, self.repo / package / suffix)
                    written.append(f"{package}/{suffix}")
        self.log.write(f"js build: wrote {len(written)} dist artifact(s)")
        return written

    def _verify_loads(self) -> Result:
        """Load all three packages the way a consumer does, and check the version.

        This is the build's exit status.  Run as a subprocess through `vlib.run`
        so it is in the command ledger with its output, like every other step.
        """
        want = ""
        for crate in (self.contract.get("state_b") or {}).get("crates") or []:
            if crate.get("name") == "acorn":
                want = str(crate.get("version") or "")
        script = (
            "const path = require('path');\n"
            "const repo = process.argv[1], want = process.argv[2];\n"
            "const names = %r;\n"
            "for (const name of names) {\n"
            "  const mod = require(path.join(repo, name));\n"
            "  const n = Object.keys(mod).length;\n"
            "  if (!n) throw new Error(name + ' loaded but exported nothing');\n"
            "  console.log(name + ': ' + n + ' exports');\n"
            "}\n"
            "const acorn = require(path.join(repo, 'acorn'));\n"
            "if (want && acorn.version !== want)\n"
            "  throw new Error('acorn reports ' + acorn.version + ', contract pins ' + want);\n"
            "console.log('acorn version ' + acorn.version);\n"
        ) % (list(JS_PACKAGES),)
        return vlib.run(
            [str(self.node), "-e", script, str(self.repo), want],
            # cwd is the workspace, not the tree: from inside the repository
            # `require("acorn")` would resolve off the current directory whether
            # the workspace links existed or not, and those links are what the
            # build just wrote and what this is checking.
            cwd=self.workspace, env=self.env(), timeout=METADATA_TIMEOUT,
            log=self.log, label="js-build-verify",
        )

    # -- the install -------------------------------------------------------

    def install(self) -> Result:
        if self.prefix.exists():
            shutil.rmtree(self.prefix, ignore_errors=True)
        result = self._install_to(self.prefix, label="install")
        self.outcome.install = result
        return result

    def _install_to(self, target: Path, *, label: str) -> Result:
        """The analogue of `make install PREFIX=<dir>`: the declared inventory.

        Every path the contract declares, at the mode it declares, and nothing
        else -- `install-inventory` grades "nothing extra" too.  The two native
        entries become launchers rather than ELF binaries, which is why
        `install-modes` and `install-standalone` are skipped by name; the four
        documents are the tree's own files, copied.

        Returns a Result so this reports like any other build step, including
        failing with a reason in `stderr` when a document the contract declares is
        not in the tree.
        """
        problems: list[str] = []
        if not self.outcome.built:
            problems.append("the build did not succeed, so there is nothing to install")
        target.mkdir(parents=True, exist_ok=True)
        installed: list[str] = []
        if not problems:
            for entry in (self.contract.get("state_b") or {}).get("install_inventory") or []:
                relpath = str(entry["path"])
                dest = target / relpath
                dest.parent.mkdir(parents=True, exist_ok=True)
                if entry.get("native"):
                    dest.write_text(self._launcher(dest.name), encoding="utf-8")
                else:
                    source = self._document_source(relpath)
                    if source is None or not source.is_file():
                        problems.append(f"{relpath}: no source file in the tree")
                        continue
                    shutil.copy2(source, dest)
                dest.chmod(int(str(entry.get("mode", "0644")), 8))
                installed.append(relpath)
        if not problems:
            problems.extend(self._smoke(target, label=label))
        result = Result(
            argv=["<js-install>", f"PREFIX={target}"], cwd=str(self.repo),
            returncode=1 if problems else 0,
            stdout=("installed " + ", ".join(installed)).encode(),
            stderr="; ".join(problems).encode(),
            duration=0.0,
        )
        self.log.record(result, label)
        self.log.write(
            f"  $ js install PREFIX={target} -> "
            f"{'ok' if result.ok else '; '.join(problems[:2])}"
        )
        return result

    def _smoke(self, target: Path, *, label: str) -> list[str]:
        """Put one real request through each installed launcher.

        Writing a launcher always succeeds -- it is a text file -- so writing one
        is not evidence that anything runs.  A launcher naming a tree that does not
        load, an interpreter that is not there, or a protocol adapter that cannot
        find its own dependencies is written just as successfully and fails on first
        use, which is a defect the install check has to catch here rather than let
        thirteen behavioural modules discover one case at a time.

        `version` for the probe, `--version` for the CLI: each exercises the whole
        chain -- shell script, interpreter, adapter, the tree's own load path -- and
        neither depends on a fixture.  Run under a scrubbed environment with the
        launcher's own absolute paths, which is how the graded runs reach it.
        """
        problems: list[str] = []
        probe = target / "bin/acorn-probe"
        answered = vlib.run(
            [str(probe)], cwd=self.workspace, env=vlib.base_env(),
            timeout=METADATA_TIMEOUT, log=self.log, label=f"{label}-probe-smoke",
            stdin_data=b'{"id":1,"op":"version"}\n',
        )
        if not answered.ok:
            problems.append(f"bin/acorn-probe did not run: {answered.tail(4, 300)}")
        elif b'"ok":true' not in answered.stdout.replace(b" ", b""):
            problems.append(
                f"bin/acorn-probe ran but answered no version: "
                f"{answered.text()[:200]!r}"
            )
        # `--help`, not `--version`: acorn's CLI has no version flag and answers
        # any unknown one through `help(1)`, so `--version` would exit 1 on the
        # original and read as a broken install.  `--help` exits 0 and still
        # reaches the parser's module body, which is the part that can fail.
        cli = vlib.run(
            [str(target / "bin/acorn"), "--help"], cwd=self.workspace,
            env=vlib.base_env(), timeout=METADATA_TIMEOUT, log=self.log,
            label=f"{label}-cli-smoke",
        )
        if not cli.ok:
            problems.append(f"bin/acorn --help failed: {cli.tail(4, 300)}")
        return problems

    def _document_source(self, relpath: str) -> Path | None:
        """Where in the tree the document at this install path comes from.

        `docs-installed` compares the installed bytes against the file they came
        from, so this mapping and that check have to agree about the source. It is
        derived from the install path rather than tabulated: `share/doc/acorn/X`
        is the root's X, and `share/licenses/<pkg>/LICENSE` is that package's.
        """
        parts = relpath.split("/")
        if parts[:2] == ["share", "doc"] and len(parts) == 4:
            return self.repo / parts[3]
        if parts[:2] == ["share", "licenses"] and len(parts) == 4:
            candidate = self.repo / parts[2] / parts[3]
            if candidate.is_file():
                return candidate
            # acorn-loose and acorn-walk carry no LICENSE of their own in the npm
            # tree; upstream publishes them under the root's licence.  Falling
            # back to it is what the packages' own `package.json` licence field
            # claims, and `docs-installed` then compares against that same file.
            return self.repo / parts[3]
        return None

    def _launcher(self, name: str) -> str:
        """A `bin/` entry: exec node against this tree.

        Pinned to this repository by absolute path.  The alternative -- resolving
        the tree at run time -- would let a launcher that outlived its tree find
        somebody else's acorn, and `reinstall-elsewhere` compares two prefixes
        that must hold the same thing.
        """
        if name.endswith("probe"):
            # The probe protocol is the harness's, not acorn's: State A ships no
            # implementation of it, and writing one here would be writing an
            # answer key.  `reference.js` is the protocol's recorded definition --
            # the same file every expectation was frozen through -- and
            # ACORN_REFERENCE_REPO is its documented way of being pointed at a
            # tree.  So what answers is the harness's adapter over *this* tree's
            # parser: edit the tree's source and the answers change, which is what
            # makes this a measurement of the tree and not of the adapter.
            return (
                "#!/bin/sh\n"
                f'ACORN_REFERENCE_REPO="{self.repo}" '
                f'exec "{self.node}" "{self.reference_js}" "$@"\n'
            )
        return (
            "#!/bin/sh\n"
            f'exec "{self.node}" "{self.repo}/acorn/bin/acorn" "$@"\n'
        )

    @property
    def reference_js(self) -> Path:
        """The probe protocol's recorded implementation, in this image."""
        if self.assets is None:
            raise SystemExit(
                "verifier defect: JsBuilder was constructed without an assets "
                "root, so the probe protocol cannot be located. Construct it "
                "through build.make_builder(..., assets=...)."
            )
        return self.assets / "reference.js"

    # -- the probes --------------------------------------------------------

    def metadata(self) -> Result:
        """There is no crate graph, and no version of this that would be honest.

        The Rust builder reads `cargo metadata`; there is no analogue, and every
        structural check that consumes it is skipped by name.  Returning a failed
        Result rather than raising keeps `run_all`'s shape identical for both
        builders.
        """
        result = Result(
            argv=["<no-cargo-metadata>"], cwd=str(self.repo), returncode=1,
            stdout=b"", duration=0.0,
            stderr=b"this tree has no Cargo.toml, so there is no crate graph to read",
        )
        self.outcome.metadata = result
        return result

    def probe_rebuild(self) -> Result:
        """A second build must not redo work.

        Genuinely true here rather than vacuously: the bridges are written from
        the same source to the same paths, so nothing is compiled, and the check
        that reads this looks for cargo's `Compiling` lines -- of which there are
        none, because there is nothing to compile.  Recorded with that stated, so
        a reader of the report is not left to infer which of the two it was.
        """
        result = self._verify_loads()
        self.outcome.extra["rebuild"] = result
        return result

    def probe_clean(self) -> Result:
        """The analogue of `make clean`: remove what the build wrote.

        What it removed is recorded now, for the same reason the Rust builder
        records it now -- the next build puts it back.
        """
        before = sorted(
            f"{pkg}/{rel}" for pkg, rel, _ in JS_DIST_BRIDGES
            if (self.repo / pkg / rel).is_file()
        )
        for package in JS_PACKAGES:
            shutil.rmtree(self.repo / package / "dist", ignore_errors=True)
        shutil.rmtree(self.repo / "node_modules", ignore_errors=True)
        after = sorted(
            f"{pkg}/{rel}" for pkg, rel, _ in JS_DIST_BRIDGES
            if (self.repo / pkg / rel).is_file()
        )
        result = Result(
            argv=["<js-clean>"], cwd=str(self.repo), returncode=0,
            stdout=f"removed {len(before)} dist artifact(s) and node_modules".encode(),
            stderr=b"", duration=0.0,
        )
        self.log.record(result, "clean")
        self.outcome.extra["clean"] = result
        self.outcome.observed["clean"] = {
            "binaries_before": before,
            "binaries_after": after,
            "target_exists_after": any(
                (self.repo / p / "dist").is_dir() for p in JS_PACKAGES
            ),
        }
        return result

    def probe_reinstall(self, scratch: Path) -> Result:
        target = scratch / "install2"
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        result = self._install_to(target, label="reinstall")
        self.outcome.extra["reinstall"] = result
        self.outcome.observed["reinstall"] = {
            "prefix": str(target),
            "files": sorted(
                str(p.relative_to(target))
                for p in target.rglob("*") if p.is_file() and not p.is_symlink()
            ),
        }
        return result

    def probe_offline_fetch(self) -> Result:
        """No dependency graph to fetch, offline or otherwise."""
        result = Result(
            argv=["<no-cargo-fetch>"], cwd=str(self.repo), returncode=1,
            stdout=b"", duration=0.0,
            stderr=b"this tree has no Cargo.toml, so there is nothing to fetch",
        )
        self.outcome.extra["offline_fetch"] = result
        return result

    def probe_install_from_clean(self, scratch: Path) -> Result:
        """Install on a tree that has just been cleaned.

        Runs after `probe_clean`, so `dist/` really is gone -- which means this
        has to rebuild before installing, exactly as a Makefile whose `install`
        depends on `build` does.  A builder that skipped the rebuild here would
        install launchers pointing at a tree that no longer loads, and the check
        downstream would pass on a prefix that does not work.
        """
        target = scratch / "install3"
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        rebuilt = self.build()
        if not rebuilt.ok:
            self.outcome.extra["install_from_clean"] = rebuilt
            self.outcome.observed["install_from_clean"] = {
                "prefix": str(target), "files": []
            }
            return rebuilt
        result = self._install_to(target, label="install-from-clean")
        self.outcome.extra["install_from_clean"] = result
        self.outcome.observed["install_from_clean"] = {
            "prefix": str(target),
            "files": sorted(
                str(p.relative_to(target))
                for p in target.rglob("*") if p.is_file() and not p.is_symlink()
            ),
        }
        return result

    def run_all(self) -> BuildOutcome:
        """Build then install.  No cargo config, no lockfile observation.

        The Rust builder writes a `CARGO_HOME/config.toml` and records whether a
        `Cargo.lock` was committed before the build could write one.  Neither has
        an analogue here, and `lockfile-present` is skipped by name rather than
        shown a `committed: false` it would correctly report as a defect.
        """
        self.log.section("build submission (javascript, pre-migration tree)")
        self.discard_artifacts()
        result = self.build()
        if not result.ok:
            self.log.write("js build failed; skipping install")
            return self.outcome
        self.install()
        self.metadata()
        return self.outcome


def read_shim_ledger(path: Path) -> list[dict]:
    """Every interpreter invocation the build attempted.

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
            import json
            out.append(json.loads(line))
        except ValueError:
            out.append({"tool": "<unparseable>", "raw": line[:400]})
    return out
