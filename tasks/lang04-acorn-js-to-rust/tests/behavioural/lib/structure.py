#!/usr/bin/env python3
"""Grades the deliverable as a package rather than as a parser.

The differential asks whether the port is correct.  This asks whether it is a
thing a distribution could ship: whether `make install PREFIX=...` puts exactly
the declared files in the declared places with the declared modes, whether the
workspace is the four crates at their pinned versions, whether the installed
binaries still work once the build tree is gone, and whether the parts of the
repository that had to survive the migration did.

Every expectation here is read from source-contract.json -- the same copy the
agent was handed -- rather than written into this file.  A check that graded
against a constant here could drift from the instruction, and a submission would
be failed for obeying the document it was given.

These cases are scored, not gates.  A port with a Makefile that hardcodes its
prefix is worse than one without, but it is still a port; the audit gates are
where "this is not a port at all" is decided.
"""

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path

import build as buildmod
import catalog
import elflib
import vlib
from build import BuildOutcome
from vlib import CaseOutcome, Log

# `make clean` and the standalone-install probe are the only checks that mutate
# anything, and both work on copies.  Nothing here touches the submission tree
# that the audit gates will walk.
PROBE_TIMEOUT = 120.0

# Libraries a statically-linked-against-libc Rust binary legitimately needs on
# glibc Linux.  Anything else in NEEDED is a third-party shared library, which
# the contract forbids -- the point of "std, core and alloc only" is that the
# delivered binary has no dependency the platform does not already provide.
ALLOWED_NEEDED = {
    "libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1",
    "libgcc_s.so.1", "ld-linux-x86-64.so.2", "libutil.so.1",
}


# --------------------------------------------------------------------------- #
# What a pre-migration tree can be asked
# --------------------------------------------------------------------------- #
#
# Every case in this module reads something the build produced.  For ten of the
# seventeen that thing is a deliverable -- a prefix with the declared files in it,
# a command that runs, a probe that answers its version -- and acorn's own npm tree
# produces all ten, which `build.JsBuilder` is what makes possible.
#
# For the other seven the thing produced is specifically a Rust artifact: an ELF
# header, a crate graph, a Cargo.lock, a target/release directory.  A JavaScript
# tree does not have those, and does not have them absolutely: `bin/acorn` is not
# an ELF file with the wrong contents, it is a shell script, in the same way and
# for the same reason that a Rust binary has no `package.json`.  Asking those seven
# anyway produces seven failures whose text says the pre-migration tree is missing
# something -- and the thing it is missing is the migration.
#
# Why this matters beyond tidiness: State A is the oracle.  Every frozen
# expectation in this suite was computed by running this tree, so what it scores
# here is this stage's own calibration, and a scale with no 1.0 cannot be read.  A
# submission scoring 0.62 and the oracle scoring 0.62 look identical.
#
# `skip`, not `fail`, and it is a recording choice rather than a scoring one.
# `pooled` charges a skipped case to the denominator, so these seven cost their
# share of the module either way.  The verdict is `skip` because the two are not the
# same finding: `fail` would assert that a Rust delivery was examined and found
# wrong, and what happened is that there is no Rust delivery to examine.  A reader
# of the transcript needs that distinction; the score does not change with it.
#
# Which means this is charged to a tree that did not migrate, deliberately.  The
# migration is the task, so a case about the Rust delivery is answerable by any
# submission that did it -- unlike the corpus cases `freeze.check_unportable` finds,
# where the *reference* has no answer and nobody can be asked at all.  Measured:
# every one of the 520 graded cells produced a Rust tree, so no published number
# moves; what reaches this path is an unported submission and the identity run.
#
# Applied per submission and at grading time, never as a catalog weight.  A Rust
# submission is asked all seventeen exactly as before -- `outcome.language` is
# `rust` for any tree carrying evidence of a port, which `build.port_evidence`
# defines as a manifest, a lockfile, a root Makefile or a single `.rs` file
# anywhere, and `detect_language` checks for that first, so nothing here can lower
# a port's score.  It was a `Cargo.toml`-at-the-root check once, and a tree with
# sixteen `.rs` files under `acorn/src/` was measured taking this table's ten-case
# split; the reason that mattered is not the split but the builder it comes with.

#: The ten checks a pre-migration tree answers on its own terms.  Each is a real
#: question with a real answer, verified by running them: the source builds, the
#: install produces exactly the declared inventory, a second build is a no-op,
#: `clean` removes what the build made, a second prefix gets the same files,
#: install works on a cleaned tree, the probe reports its version, both installed
#: commands run from a bare PATH, and the installed documents match their sources.
JS_STRUCT_KEEP = frozenset({
    "build-succeeds", "install-succeeds", "install-inventory",
    "rebuild-is-noop", "clean-target", "reinstall-elsewhere",
    "install-from-clean", "probe-version", "binaries-executable",
    "docs-installed",
})

#: Every other check, with the reason it cannot be asked.  Grouped by the reason,
#: because three separate sentences saying "this reads an ELF header" would be
#: three chances to say it differently; the group is the reason and the check names
#: are what it covers.
JS_STRUCT_SKIP_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("this reads the installed file as an ELF executable -- its type, its "
     "interpreter, its NEEDED list, whether it still runs once the build tree is "
     "deleted -- and a JavaScript tree installs a launcher over an interpreter, "
     "so there is no such artifact to read rather than a bad one. What these "
     "stand for is measured on this path too: `install-inventory` checks the same "
     "six paths exist and nothing else does, and `binaries-executable` runs both "
     "of them from a bare PATH",
     ("install-modes", "install-standalone", "no-runtime-deps")),
    ("this reads the crate graph `cargo metadata` resolves, and a tree with no "
     "Cargo.toml has no crate graph. The version question behind it is answered "
     "on this path by `probe-version`, which asks the built artifact what version "
     "it is rather than asking a manifest what it claims",
     ("workspace-members", "crate-versions")),
    ("this reads an artifact of cargo's own build layout -- target/release, and "
     "the Cargo.lock cargo writes beside it -- which exists only where cargo ran",
     ("release-artifacts", "lockfile-present")),
)

#: check name -> reason, flattened from the groups above.
JS_STRUCT_SKIP: dict[str, str] = {
    name: reason
    for reason, names in JS_STRUCT_SKIP_GROUPS
    for name in names
}

# A check in both tables is a table that contradicts itself about what it
# measures.  Asserted at import rather than left to the round-trip check in
# freeze.py, because that one compares the union against the catalog and a name in
# both tables is in the union either way.
_STRUCT_OVERLAP = JS_STRUCT_KEEP & set(JS_STRUCT_SKIP)
if _STRUCT_OVERLAP:
    raise SystemExit(
        f"verifier defect: structural check(s) {sorted(_STRUCT_OVERLAP)} are both "
        f"kept and skipped for a pre-migration tree"
    )


def skip_reason(check: str, language: str) -> str:
    """Why `check` is not asked of a `language` tree, or "" if it is asked.

    The language branch lives here rather than at the call site so that there is
    one decision to test.  Split between the two -- a condition at the call site and
    a table here -- the self-check could test the table while the runner consulted
    it under the wrong condition, and the two would never be compared.
    """
    if language == buildmod.LANG_RUST:
        return ""
    return js_struct_skip_reason(check)


def js_struct_skip_reason(check: str) -> str:
    """Why `check` is not asked of a pre-migration tree, or "" if it is asked."""
    if check in JS_STRUCT_KEEP:
        return ""
    reason = JS_STRUCT_SKIP.get(check)
    if reason is None:
        # Not classified.  Refused rather than defaulted either way: defaulting to
        # asked makes a Rust-only check fail on a JavaScript tree and read as the
        # pre-migration implementation having lost a behaviour it never had, and
        # defaulting to skipped silently drops a case that may well have been
        # measurable.
        raise SystemExit(
            f"verifier defect: structural check {check!r} is in neither "
            f"JS_STRUCT_KEEP nor JS_STRUCT_SKIP, so there is no answer to whether "
            f"a pre-migration tree can be asked it"
        )
    return reason


class StructureAuditor:
    """Runs the declared structural cases against one built submission."""

    def __init__(
        self,
        repo: Path,
        outcome: BuildOutcome,
        contract: dict,
        scratch: Path,
        log: Log,
        *,
        probe_answer=None,
    ) -> None:
        # `repo` is here for the build and install targets to run *in*, and for
        # `docs-installed` to compare an installed copy against the file it came
        # from.  No check in this file reads it to decide whether the port is
        # organised correctly; State A's tree is deliberately not a parameter at
        # all, so a check here cannot start diffing the submission against it.
        self.repo = repo
        self.outcome = outcome
        self.contract = contract
        self.scratch = scratch
        self.log = log
        self.scratch.mkdir(parents=True, exist_ok=True)
        # A callable taking an NDJSON request dict and returning the parsed
        # response, or None if the probe could not be reached.  Injected rather
        # than built here so every structural case that needs to ask the probe
        # something shares one session, and so this module does not have to know
        # how the probe is launched -- driver.py owns that, including closing it.
        # The differential phase runs later and opens its own.
        self.probe_answer = probe_answer
        self.state_b = contract.get("state_b") or {}
        self.inventory = self.state_b.get("install_inventory") or []
        self.crates = self.state_b.get("crates") or []
        self.policy = contract.get("native_code_policy") or {}
        self._packages: dict[str, dict] | None = None

    # -- helpers ----------------------------------------------------------

    @property
    def prefix(self) -> Path:
        return self.outcome.prefix

    def packages(self) -> dict[str, dict]:
        """Crate name -> cargo metadata package, parsed once.

        Read from `cargo metadata` rather than from Cargo.toml: a workspace that
        sets versions through `workspace.package` inheritance has no version
        literal in the member manifests at all, and parsing the TOML by hand
        would report every crate as unversioned.
        """
        if self._packages is not None:
            return self._packages
        self._packages = {}
        result = self.outcome.metadata
        if result is None or not result.ok:
            return self._packages
        try:
            payload = json.loads(result.stdout.decode("utf-8", "replace"))
        except ValueError:
            return self._packages
        for package in payload.get("packages") or []:
            self._packages[package.get("name", "")] = package
        return self._packages

    def installed_files(self) -> dict[str, Path]:
        """Every regular file under the prefix, by prefix-relative path."""
        out: dict[str, Path] = {}
        if not self.prefix.is_dir():
            return out
        for path in sorted(self.prefix.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            out[str(path.relative_to(self.prefix))] = path
        return out

    def bare_env(self) -> dict:
        """An environment with nothing of the build in it.

        Deliberately not the build environment: an installed binary that only
        runs with the build's PATH, CARGO_HOME or LD_LIBRARY_PATH set is not
        installed, and running it with those still set would hide that.
        """
        return vlib.base_env(PATH="/usr/local/bin:/usr/bin:/bin")

    # -- the build, as published ------------------------------------------

    def check_build_succeeds(self) -> tuple[bool, str]:
        result = self.outcome.build
        argv = " ".join(self.contract["build_contract"]["build"])
        if result is None:
            return False, f"`{argv}` was never run"
        if result.timed_out:
            return False, f"`{argv}` timed out"
        if not result.ok:
            return False, f"`{argv}` exited {result.returncode}: {result.tail()}"
        return True, f"`{argv}` succeeded in {result.duration:.1f}s"

    def check_install_succeeds(self) -> tuple[bool, str]:
        result = self.outcome.install
        if result is None:
            if not self.outcome.built:
                return False, "not attempted: the build failed"
            return False, "`make install` was never run"
        if not result.ok:
            return False, f"`make install` exited {result.returncode}: {result.tail()}"
        return True, "`make install PREFIX=...` succeeded"

    def check_install_inventory(self) -> tuple[bool, str]:
        """Exactly the declared paths, nothing missing and nothing extra.

        "Nothing extra" is graded because the instruction says *exactly*.  An
        install that also drops a `lib/libacorn.a` or a stray `.d` file is a
        packaging defect: a distribution would ship it, and nothing else in the
        contract accounts for it.
        """
        if not self.prefix.is_dir():
            return False, f"nothing was installed: {self.prefix} does not exist"
        declared = {entry["path"] for entry in self.inventory}
        found = set(self.installed_files())
        missing = sorted(declared - found)
        extra = sorted(found - declared)
        if missing or extra:
            parts = []
            if missing:
                parts.append(f"missing {len(missing)}: {', '.join(missing[:6])}")
            if extra:
                parts.append(f"undeclared {len(extra)}: {', '.join(extra[:6])}")
            return False, "; ".join(parts)
        return True, f"all {len(declared)} declared paths installed, nothing else"

    def check_install_modes(self) -> tuple[bool, str]:
        """The declared mode on each installed file, and ELF where declared."""
        files = self.installed_files()
        problems: list[str] = []
        for entry in self.inventory:
            relpath = entry["path"]
            path = files.get(relpath)
            if path is None:
                problems.append(f"{relpath}: not installed")
                continue
            want = int(entry.get("mode", "0644"), 8)
            got = stat.S_IMODE(path.stat().st_mode)
            if got != want:
                problems.append(f"{relpath}: mode {got:04o}, contract says {want:04o}")
            if entry.get("native") and not elflib.is_elf(path):
                head = path.read_bytes()[:60]
                problems.append(
                    f"{relpath}: not an ELF executable (starts {head[:24]!r})"
                )
        if problems:
            return False, "; ".join(problems[:8])
        return True, f"{len(self.inventory)} installed paths have their declared modes"

    def check_rebuild_is_noop(self) -> tuple[bool, str]:
        """A second `make build` must not rebuild the workspace.

        Cargo prints `Compiling` only when it actually compiles, so its own
        output is the measurement.  A Makefile that reruns cargo unconditionally
        is fine -- cargo will decide there is nothing to do.
        """
        result = self.outcome.extra.get("rebuild")
        if result is None:
            return False, "the rebuild probe was not run"
        if not result.ok:
            return False, f"a second `make build` failed: {result.tail()}"
        blob = (result.stdout + result.stderr).decode("utf-8", "replace")
        compiled = [
            line.strip() for line in blob.splitlines()
            if line.strip().startswith("Compiling ")
        ]
        if compiled:
            return False, (
                f"a second `make build` recompiled {len(compiled)} crate(s): "
                f"{'; '.join(compiled[:4])}"
            )
        return True, "a second `make build` compiled nothing"

    def check_clean_target(self) -> tuple[bool, str]:
        """`make clean` must succeed and must remove the release artifacts.

        Declared optional by the contract, so its absence is not a failure -- but
        a `clean` that exists and leaves the binaries behind is.
        """
        result = self.outcome.extra.get("clean")
        if result is None:
            return False, "the clean probe was not run"
        blob = (result.stdout + result.stderr).decode("utf-8", "replace").lower()
        if not result.ok:
            if "no rule to make target" in blob or "no targets" in blob:
                return True, "no `clean` target, which the contract allows"
            return False, f"`make clean` exited {result.returncode}: {result.tail()}"
        observed = self.outcome.observed.get("clean") or {}
        before = observed.get("binaries_before") or []
        after = observed.get("binaries_after") or []
        if not before:
            # Nothing was there to remove, so this says nothing about `clean`
            # either way.  Reported as a pass with the reason visible rather than
            # as a silent one: a reader of the report should not have to wonder
            # which of the two it was.
            return True, (
                "`make clean` succeeded; target/release held no binaries when it "
                "ran, so there was nothing to remove"
            )
        if after:
            return False, (
                f"`make clean` succeeded but left {', '.join(after)} in "
                f"target/release"
            )
        return True, (
            f"`make clean` removed {', '.join(before)} from target/release"
        )

    def check_reinstall_elsewhere(self) -> tuple[bool, str]:
        """A second prefix must get the same tree.

        A prefix substituted at build time rather than read at install time is
        invisible when only one prefix is ever used, and it breaks every
        packaging system that stages into a temporary root.
        """
        result = self.outcome.extra.get("reinstall")
        if result is None:
            return False, "the reinstall probe was not run"
        if not result.ok:
            return False, f"installing to a second prefix failed: {result.tail()}"
        # The tree the probe saw, from the probe, rather than a path guessed here:
        # this check must not depend on agreeing with build.py about where the
        # second prefix went.
        observed = self.outcome.observed.get("reinstall") or {}
        got = set(observed.get("files") or [])
        if not got:
            return False, (
                f"the second install reported success but produced no files under "
                f"{observed.get('prefix', 'the second prefix')}"
            )
        first = set(self.installed_files())
        if first != got:
            missing = sorted(first - got)
            extra = sorted(got - first)
            return False, (
                f"the second prefix differs: missing {missing[:4]}, "
                f"extra {extra[:4]}"
            )
        return True, f"a second prefix got the same {len(got)} files"

    def check_install_from_clean(self) -> tuple[bool, str]:
        """`make install PREFIX=<dir>` must work on an unbuilt tree.

        The probe runs after `make clean`, so target/release really is empty. What
        this catches is an `install` target that does not depend on `build`: it
        works for anyone who types the two commands in order, and fails for every
        packaging script that runs only the second one.
        """
        result = self.outcome.extra.get("install_from_clean")
        if result is None:
            return False, "the install-from-clean probe was not run"
        observed = self.outcome.observed.get("install_from_clean") or {}
        if not result.ok:
            return False, (
                f"`make install` on a cleaned tree exited {result.returncode}: "
                f"{result.tail()}"
            )
        got = set(observed.get("files") or [])
        declared = {entry["path"] for entry in self.inventory}
        short = sorted(declared - got)
        if short:
            return False, (
                f"`make install` on a cleaned tree exited 0 but installed "
                f"{len(got)} of {len(declared)} paths (missing {', '.join(short[:5])})"
            )
        return True, (
            f"`make install` on a cleaned tree rebuilt and installed all "
            f"{len(declared)} paths"
        )

    # -- the workspace, as declared ---------------------------------------

    def check_workspace_members(self) -> tuple[bool, str]:
        packages = self.packages()
        if not packages:
            return False, "cargo metadata did not run or did not parse"
        want = {crate["name"] for crate in self.crates}
        missing = sorted(want - set(packages))
        if missing:
            return False, (
                f"the workspace has {sorted(packages)}; the contract requires "
                f"{sorted(want)} (missing {missing})"
            )
        # Dependency direction, as declared.  acorn-loose depending on acorn is
        # part of the shape; a port that merged them has changed the deliverable.
        problems: list[str] = []
        for crate in self.crates:
            for needed in crate.get("depends_on") or []:
                deps = {
                    d.get("name") for d in packages[crate["name"]].get("dependencies") or []
                }
                if needed not in deps:
                    problems.append(f"{crate['name']} does not depend on {needed}")
        if problems:
            return False, "; ".join(problems[:6])
        return True, f"{len(want)} crates, with the declared dependencies"

    def check_crate_versions(self) -> tuple[bool, str]:
        packages = self.packages()
        if not packages:
            return False, "cargo metadata did not run; cannot read versions"
        problems: list[str] = []
        for crate in self.crates:
            package = packages.get(crate["name"])
            if package is None:
                problems.append(f"{crate['name']}: absent")
                continue
            if package.get("version") != crate["version"]:
                problems.append(
                    f"{crate['name']}: {package.get('version')}, "
                    f"contract pins {crate['version']}"
                )
        if problems:
            return False, "; ".join(problems)
        return True, "every crate reports its JavaScript version"

    def check_release_artifacts(self) -> tuple[bool, str]:
        """The build must leave the two binaries the instruction names.

        A `make build` that exits 0 has not necessarily produced anything, and a
        Makefile that puts its output somewhere else -- a custom `--target-dir`, a
        differently named binary -- satisfies every other check here while
        breaking the one path the instruction spells out.

        Read from what the build observed rather than from the tree, because
        `make clean` runs afterwards and takes the evidence with it.
        """
        if not self.outcome.built:
            return False, "not attempted: the build failed"
        observed = self.outcome.observed.get("release_artifacts")
        if not observed:
            # `not observed`, not `is None`.  An empty dict reaches `missing == []`
            # and passes with "target/release holds " -- a sentence naming nothing,
            # produced by a build that recorded nothing.  The two are one failure:
            # this case is answered by the build's record of what it produced, and
            # there is no such record.
            return False, (
                "the build recorded no target/release inventory, so what it "
                "produced is unknown"
            )
        missing = sorted(name for name, present in observed.items() if not present)
        if missing:
            return False, (
                f"`make build` exited 0 but target/release holds no "
                f"{', '.join(missing)}"
            )
        return True, (
            f"target/release holds {', '.join(sorted(observed))} after the build"
        )

    def check_lockfile_present(self) -> tuple[bool, str]:
        """Cargo.lock must be committed and must resolve to workspace members only.

        A workspace of binaries is an application, so its lockfile belongs in the
        repository.  Its contents are also the shortest proof that nothing
        external is in the graph.

        The allowed set is the workspace's own members, not the four crates the
        contract names.  A submission that added a helper member -- an `xtask`, a
        shared internal crate -- has not taken a dependency on anything, and
        failing it here would be grading a structure the instruction never
        forbade.
        """
        # Whether it was *committed* is what the build recorded before it ran.
        # The file on disk now proves nothing: cargo writes one during the build,
        # so a submission that never had a lockfile has one by the time this runs.
        observed = self.outcome.observed.get("lockfile") or {}
        if observed and not observed.get("committed"):
            return False, (
                "no Cargo.lock was committed; the one in the tree now was written "
                "by the graded build"
            )
        path = self.repo / "Cargo.lock"
        if not path.is_file():
            return False, "no Cargo.lock at the workspace root"
        text = path.read_text(encoding="utf-8", errors="replace")
        names = sorted(
            line.split("=", 1)[1].strip().strip('"')
            for line in text.splitlines()
            if line.startswith("name = ")
        )
        declared = {crate["name"] for crate in self.crates}
        # cargo metadata --no-deps lists exactly the members; the declared names
        # are unioned in so a failed metadata run cannot turn this into a
        # false accusation of vendoring.
        allowed = declared | set(self.packages())
        extra = sorted(set(names) - allowed)
        if extra:
            return False, (
                f"Cargo.lock resolves {len(extra)} package(s) from outside the "
                f"workspace: {', '.join(extra[:8])}"
            )
        # Whether the declared crates are *in* the lock is not asked here.  A
        # missing crate is a missing crate, which `workspace-members` and
        # `crate-versions` already grade; repeating it would charge the rename of
        # one crate to three cases.
        return True, (
            f"Cargo.lock is present and resolves only the {len(set(names))} "
            f"workspace members"
        )

    # -- what the binaries say about themselves ---------------------------

    def check_install_standalone(self) -> tuple[bool, str]:
        """The installed binaries must not depend on the build tree.

        The failure this catches is an `install` that writes a shell script
        pointing back at `target/release`, or a binary with a runpath or an
        embedded absolute path into the build directory.  All of them work
        perfectly until someone deletes the source tree, which is the first thing
        a packager does.

        The build tree is *not* deleted to find out.  The audit gates still
        have to walk this tree, and a structural check that damaged the artifact
        being measured could turn one lost point into a lost phase.  What is done
        instead: the binaries are copied out to a directory with nothing else in
        it, run from there with a scrubbed environment and a cwd outside the
        repository, and inspected for any reference to the build tree.  The three
        ways the dependency can exist are each covered -- a wrapper script is not
        an ELF file (`install-modes`), a runpath is in the dynamic section, and a
        hardcoded path is in `.rodata`.
        """
        files = self.installed_files()
        binaries = [e["path"] for e in self.inventory if e.get("native")]
        if not binaries:
            return False, "the contract declares no native binaries"
        island = self.scratch / "standalone"
        if island.exists():
            shutil.rmtree(island, ignore_errors=True)
        island.mkdir(parents=True)

        problems: list[str] = []
        needles = {str(self.repo), str(self.repo / "target")}
        for relpath in binaries:
            path = files.get(relpath)
            if path is None:
                problems.append(f"{relpath}: not installed")
                continue
            name = Path(relpath).name
            copied = island / name
            shutil.copy2(path, copied)

            # `--help` for the CLI: it exits 0 without reading input, and printing
            # usage is the one thing every acorn CLI does.  The probe is given EOF,
            # on which the protocol says it exits 0.
            if name.endswith("probe"):
                argv, stdin = [str(copied)], b""
            else:
                argv, stdin = [str(copied), "--help"], None
            result = vlib.run(argv, cwd=island, env=self.bare_env(),
                              timeout=PROBE_TIMEOUT, stdin_data=stdin)
            if result.timed_out:
                problems.append(f"{name}: did not exit within {PROBE_TIMEOUT:.0f}s")
            elif not result.ok:
                problems.append(
                    f"{name}: exited {result.returncode} when run outside the "
                    f"build tree with a bare environment: {result.tail(lines=4)}"
                )

            if not elflib.is_elf(copied):
                problems.append(f"{name}: not an ELF file, so it is a wrapper")
                continue
            try:
                obj = elflib.load(copied)
            except elflib.ElfError as exc:
                problems.append(f"{name}: unreadable ELF ({exc})")
                continue
            inside = [p for p in obj.runpath if any(n in p for n in needles)]
            if inside:
                problems.append(
                    f"{name}: runpath points into the build tree ({inside[0]})"
                )
            try:
                rodata = obj.strings_in(".rodata")
            except elflib.ElfError:
                rodata = []
            hits = sorted({s for s in rodata if any(n in s for n in needles)})
            if hits:
                problems.append(
                    f"{name}: embeds the build directory in .rodata "
                    f"({hits[0][:120]!r}), so it reads from a path that will not "
                    f"exist once the source tree is gone"
                )
        if problems:
            return False, "; ".join(problems[:4])
        return True, (
            f"{len(binaries)} installed binaries run from an empty directory with "
            f"a bare environment and hold no reference to the build tree"
        )

    def check_probe_version(self) -> tuple[bool, str]:
        """The probe's `version` op must report acorn's version.

        Asked through the protocol rather than of the binary, because the
        protocol is where the library surface is defined and `acorn --version`
        does not exist.
        """
        if self.probe_answer is None:
            return False, "no probe session was available to ask"
        want = None
        for crate in self.crates:
            if crate["name"] == "acorn":
                want = crate["version"]
        response = self.probe_answer({"id": 1, "op": "version"})
        if response is None:
            return False, "the probe did not answer a `version` request"
        if not response.get("ok"):
            return False, f"`version` returned an error: {str(response)[:200]}"
        got = response.get("result")
        if got != want:
            return False, f"the probe reports version {got!r}, expected {want!r}"
        return True, f"the probe reports {got}"

    def check_binaries_executable(self) -> tuple[bool, str]:
        """Both installed binaries must run in place, from a bare PATH.

        In place, from the prefix: this is the plain question of whether what was
        installed executes at all.  `install-standalone` asks the different
        question of whether it still executes once moved away from the build, and
        the two are separate because a binary can pass this and fail that.
        """
        files = self.installed_files()
        problems: list[str] = []
        checked = 0
        for entry in self.inventory:
            if not entry.get("native"):
                continue
            path = files.get(entry["path"])
            if path is None:
                problems.append(f"{entry['path']}: not installed")
                continue
            checked += 1
            name = path.name
            argv = [str(path), "--help"] if not name.endswith("probe") else [str(path)]
            stdin = None if not name.endswith("probe") else b""
            result = vlib.run(argv, cwd=self.scratch, env=self.bare_env(),
                              timeout=PROBE_TIMEOUT, stdin_data=stdin)
            if result.timed_out:
                problems.append(f"{name} did not exit within {PROBE_TIMEOUT:.0f}s")
            elif not result.ok:
                problems.append(
                    f"{name} exited {result.returncode}: {result.tail(lines=4)}"
                )
        if problems:
            return False, "; ".join(problems[:4])
        return True, f"{checked} installed binaries run from a bare PATH"

    def check_no_runtime_deps(self) -> tuple[bool, str]:
        """NEEDED must hold nothing beyond libc and the platform's own libraries.

        "std, core and alloc only" is a claim about the source graph; this is the
        same claim checked on the delivered artefact, where a linked third-party
        library would actually show up.

        The contract declares two native entries, so inspecting none of them
        means the install did not produce them -- not that the binaries it did
        produce are dependency-free.  Without that guard a tree that installs
        nothing reports "shared library dependencies: none" and passes.
        """
        files = self.installed_files()
        problems: list[str] = []
        seen: set[str] = set()
        declared = 0
        inspected = 0
        for entry in self.inventory:
            if not entry.get("native"):
                continue
            declared += 1
            path = files.get(entry["path"])
            if path is None or not elflib.is_elf(path):
                continue
            inspected += 1
            try:
                obj = elflib.load(path)
            except elflib.ElfError as exc:
                problems.append(f"{entry['path']}: unreadable ELF ({exc})")
                continue
            needed = obj.needed
            seen.update(needed)
            unexpected = [lib for lib in needed if lib not in ALLOWED_NEEDED]
            if unexpected:
                problems.append(
                    f"{entry['path']} needs {', '.join(sorted(unexpected))}"
                )
            # Runpath is deliberately not graded here.  It belongs to
            # `install-standalone`, and a binary with an rpath into the build tree
            # has made one mistake -- charging it to two cases would price that
            # single defect at both their weights.
        if problems:
            return False, "; ".join(problems[:6])
        if declared and not inspected:
            return False, (
                f"none of the {declared} declared native binaries was installed "
                f"as an ELF file, so there was nothing to read NEEDED from"
            )
        return True, (
            f"{inspected} native binaries inspected; shared library "
            f"dependencies: {', '.join(sorted(seen)) or 'none'}"
        )

    # -- what the install did to the documents ----------------------------

    def check_docs_installed(self) -> tuple[bool, str]:
        """The installed documents must be the repository's, byte for byte.

        An install that regenerates or truncates a licence has changed it, which
        is the one thing the licences may not have happen to them.
        """
        files = self.installed_files()
        # Which repository file each installed document should be a copy of.
        # Derived from the installed path, not hardcoded: `share/licenses/X/LICENSE`
        # comes from `X/LICENSE`, and `share/doc/acorn/README.md` from `README.md`.
        problems: list[str] = []
        checked = 0
        for entry in self.inventory:
            relpath = entry["path"]
            if entry.get("native"):
                continue
            parts = Path(relpath).parts
            if parts[:2] == ("share", "licenses"):
                source = self.repo / parts[2] / Path(relpath).name
            elif parts[:2] == ("share", "doc"):
                source = self.repo / Path(relpath).name
            else:
                continue
            installed = files.get(relpath)
            if installed is None:
                problems.append(f"{relpath}: not installed")
                continue
            if not source.is_file():
                problems.append(f"{relpath}: no {source.relative_to(self.repo)} to copy from")
                continue
            checked += 1
            if vlib.sha256_file(installed) != vlib.sha256_file(source):
                problems.append(
                    f"{relpath} differs from {source.relative_to(self.repo)}"
                )
        if problems:
            return False, "; ".join(problems[:5])
        return True, f"{checked} installed documents match their sources"

    # -- driver -----------------------------------------------------------

    def evaluate(self) -> list[CaseOutcome]:
        """Run every declared structural case, in declaration order."""
        outcomes: list[CaseOutcome] = []
        skipped = 0
        for case_id, check, weight, note in catalog.STRUCT_CASES:
            reason = skip_reason(check, self.outcome.language)
            if reason:
                skipped += 1
                outcomes.append(CaseOutcome(
                    case_id=case_id, family="structure", kind="struct",
                    passed=False, weight=weight, not_applicable=reason,
                ))
                self.log.write(f"  SKIP {case_id}: {reason[:120]}")
                continue
            method = getattr(self, f"check_{check.replace('-', '_')}", None)
            if method is None:
                # A declared case with no implementation is a defect in this
                # file, reported as a failure rather than skipped: skipping would
                # award the weight of a check that never ran.
                outcomes.append(CaseOutcome(
                    case_id=case_id, family="structure", kind="struct",
                    passed=False, weight=weight,
                    detail=f"structure.py has no check for {check!r}",
                ))
                continue
            try:
                passed, detail = method()
            except Exception as exc:  # noqa: BLE001 - one bad check must not stop the phase
                passed, detail = False, f"{type(exc).__name__}: {exc}"[:400]
                self.log.write(f"  struct {case_id} raised: {exc}")
            outcomes.append(CaseOutcome(
                case_id=case_id, family="structure", kind="struct",
                passed=passed, weight=weight,
                detail=f"{note}: {detail}" if not passed else detail,
            ))
            self.log.write(f"  {'PASS' if passed else 'FAIL'} {case_id}: {detail[:160]}")
        earned = sum(o.weight for o in outcomes if o.passed)
        # Every declared case, whether or not this tree could be asked it.
        # `pooled` charges a not-applicable case, so a log reporting the smaller
        # denominator would print a rate this module is not being given.
        total = sum(o.weight for o in outcomes)
        asked = len(outcomes) - skipped
        self.log.write(
            f"structure: {sum(1 for o in outcomes if o.passed)}/{asked} "
            f"cases asked, {earned:.1f}/{total:.1f} points"
            + (f"; {skipped} case(s) not applicable to a "
               f"{self.outcome.language} tree, charged 0 -- the migration is what "
               f"would have made them applicable" if skipped else "")
        )
        return outcomes


def run_structure_phase(
    repo: Path,
    outcome: BuildOutcome,
    contract: dict,
    scratch: Path,
    log: Log,
    *,
    probe_answer=None,
) -> list[CaseOutcome]:
    log.section("structural audit")
    if len(catalog.STRUCT_CASES) < catalog.MIN_STRUCT_CASES:
        raise RuntimeError(
            f"{len(catalog.STRUCT_CASES)} structural cases declared; the catalog "
            f"floor is {catalog.MIN_STRUCT_CASES}"
        )
    auditor = StructureAuditor(repo, outcome, contract, scratch, log,
                               probe_answer=probe_answer)
    return auditor.evaluate()
