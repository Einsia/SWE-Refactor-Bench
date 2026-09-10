#!/usr/bin/env python3
"""Grades the build and the executable it produces, as behaviour.

The frozen modules ask whether the port answers YAML correctly.  This asks the
questions no answered case can: whether `zig build` at the root produces the declared
binary with the pinned compiler, whether that binary still runs once the tree it
was built in is gone, whether it flushes a response before more input arrives,
whether it exits 0 on a closed pipe.

Every case here drives the built program or inspects the file the build wrote.
None of them opens a source file, and that boundary is deliberate: a check that
reads the submission's source is asserting something about how the port was
written, and this stage grades what it does.  The questions about the tree --
where the manifest lives, whether README.md still says `go get`, whether a Zig
cache was committed -- are stage-1 gates, judged by an agent that reads it.

Every expectation is read from source-contract.json -- the same copy the agent was
handed -- rather than written into this file.  A check that graded against a
constant here could drift from the instruction, and a submission would then be
failed for obeying the document it was given.

Two entry points, because two modules grade these in separate processes:
`run_build_cases` for the build and its artefact, `run_probe_cases` for how that
artefact behaves as a process.
"""

from __future__ import annotations

import json
import re
import selectors
import subprocess
from pathlib import Path

import build as build_lib
import catalog
import elflib
import vlib
from build import BuildOutcome
from vlib import CaseOutcome, Log

PROBE_TIMEOUT = 120.0

# How each toolchain says "the test step you asked for is not there", and how it
# says "the tests could not be compiled".  Both spellings were measured rather than
# recalled, because the check's whole job is telling those two apart from a test
# that ran and failed -- and a wrong pattern here does not fail loudly, it passes
# everything.
#
#   zig 0.14.1   missing step: `error: no step named 'test'`
#                won't compile: any line matching `error:`
#   go 1.23.4    missing step: `no packages to test` (with
#                `go: warning: "./..." matched no packages` above it), exit 1
#                won't compile: `[build failed]`, and the diagnostic lines are
#                `./x_test.go:3:27: undefined: ...` -- note that Go prints no
#                `error:` anywhere, for either a build failure or a plain
#                `--- FAIL:`, which is why this table is per-toolchain rather than
#                one shared regular expression.
TEST_STEP_PATTERNS = {
    "zig": {
        "missing": r"no step named ['\"]?test",
        "uncompilable": r"\berror:",
    },
    "go": {
        "missing": r"no packages to test|matched no packages",
        "uncompilable": r"\[build failed\]",
    },
}

# Libraries a Zig binary linked against glibc legitimately needs.  Anything else
# in NEEDED is a third-party shared library, which the contract forbids: the point
# of "the Zig standard library only" is that the delivered binary asks the platform
# for nothing the platform does not already have.
#
# A fully static binary (Zig's default for a musl target, and available for glibc
# too) has no NEEDED at all, which passes trivially.  That is correct -- the check
# is for unexpected dependencies, not for the presence of expected ones.
ALLOWED_NEEDED = {
    "libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1",
    "libgcc_s.so.1", "ld-linux-x86-64.so.2", "libutil.so.1", "libunwind.so.1",
}


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
        self.repo = repo
        self.outcome = outcome
        self.contract = contract
        self.scratch = scratch
        self.log = log
        self.scratch.mkdir(parents=True, exist_ok=True)
        # A callable taking an NDJSON request dict and returning the parsed
        # response, or None if the probe could not be reached.  Injected rather
        # than built here so the handshake check reuses the session the
        # differential already established instead of starting a second one.
        self.probe_answer = probe_answer
        self.state_b = contract.get("state_b") or {}
        # The driver the build actually resolved, not `state_b`.  Two trees are
        # valid inputs here -- the repository as handed over, which builds with Go,
        # and a finished port, which builds with Zig -- and every command, path and
        # version below differs between them.  Reading `state_b` would grade one
        # tree against the other's build.
        self.driver = build_lib.driver_of(contract, outcome)
        self.binaries = (self.driver.get("binaries")
                         or self.state_b.get("binaries") or [])
        self.policy = contract.get("native_code_policy") or {}
        self.protocol = contract.get("probe_protocol") or {}

    # -- helpers ----------------------------------------------------------

    @property
    def toolchain(self) -> str:
        return self.driver.get("toolchain") or "zig"

    @property
    def artifact(self) -> Path:
        return self.repo / self.binaries[0]["path"]

    def bare_env(self) -> dict:
        """An environment with nothing of the build in it.

        Deliberately not the build environment: a binary that only runs with the
        build's PATH or cache variables set is not a deliverable, and running it
        with those still set would hide that.
        """
        return vlib.base_env(PATH="/usr/local/bin:/usr/bin:/bin")

    # -- the build, as published -------------------------------------------

    def build_argv(self, step: str) -> str:
        """The published argv for one build step, for use in messages.

        Read from the resolved driver so a failure message names the command that
        was actually run.  A message naming `zig build` under a Go build would send
        the reader looking for a defect in the wrong place.
        """
        for entry in self.driver.get("build_steps") or []:
            if entry.get("step") == step:
                return " ".join(entry.get("invoked_as") or [])
        for entry in self.state_b.get("build_steps") or []:
            if entry.get("step") == step:
                return " ".join(entry.get("invoked_as") or [])
        return step

    def check_build_succeeds(self) -> tuple[bool, str]:
        result = self.outcome.build
        argv = self.build_argv("install")
        if result is None:
            return False, "the build was never run"
        if result.timed_out:
            return False, f"`{argv}` did not finish within the build timeout"
        if not result.ok:
            return False, f"`{argv}` exited {result.returncode}: {result.tail()}"
        return True, f"`{argv}` succeeded in {result.duration:.1f}s"

    def check_build_artifact(self) -> tuple[bool, str]:
        """The declared binary, at the declared path, as a native executable.

        The path comes from the resolved driver, and so does nothing else here: the
        format is checked because a build script can be made to produce a wasm
        module or a static library with one line -- in build.zig, or with one
        GOARCH -- and either leaves a file at the right path that nothing can run.
        ELF is checked, and the machine is checked, because a cross-compiled
        aarch64 binary is also unrunnable here.
        """
        declared = self.binaries[0]["path"]
        path = self.artifact
        if not path.is_file():
            observed = self.outcome.observed.get("artifact") or {}
            return False, (
                f"{declared} does not exist after the build"
                + (f" (observed: {observed})" if observed else "")
            )
        if not path.stat().st_mode & 0o111:
            return False, f"{declared} exists but is not executable"
        if not elflib.is_elf(path):
            head = path.read_bytes()[:4]
            return False, f"{declared} is not an ELF executable (starts {head!r})"
        try:
            obj = elflib.load(path)
        except elflib.ElfError as exc:
            return False, f"{declared} is an unreadable ELF: {exc}"
        if obj.machine not in (0x3E,):  # EM_X86_64
            return False, (
                f"{declared} is ELF for machine 0x{obj.machine:x}, not x86-64: it "
                f"cannot run where it was built"
            )
        size = path.stat().st_size
        return True, f"{declared} is a {size:,}-byte x86-64 ELF executable"

    def check_test_step_exists(self) -> tuple[bool, str]:
        """The driver's test step must be a step that runs.

        Not scored on what it reports -- the instruction says it may run zero
        tests.  What is graded is that invoking it is not an error of the "there is
        no such step" kind, and separately that the tests could at least be
        compiled: a step that cannot build is not a step that "exists and runs", so
        it is reported as a failure with the reason visible rather than silently
        passed.

        Both spellings come from `TEST_STEP_PATTERNS`, measured per toolchain.  An
        unknown toolchain fails closed rather than passing on an empty pattern,
        because a pattern that matches nothing turns this into a check that grades
        the exit code alone.
        """
        argv = self.build_argv("test")
        result = self.outcome.test_step
        if result is None:
            if self.outcome.build is not None and not self.outcome.build.ok:
                return False, "the build failed, so the test step never ran"
            return False, f"`{argv}` was never run"
        patterns = TEST_STEP_PATTERNS.get(self.toolchain)
        if patterns is None:
            return False, (
                f"this module has no test-step patterns for toolchain "
                f"{self.toolchain!r}; that is a defect in structure.py, not in the "
                f"submission"
            )
        blob = (result.stderr + result.stdout).decode("utf-8", "replace")
        if re.search(patterns["missing"], blob):
            return False, (
                f"the build declares no `test` step: " + result.tail(lines=3)
            )
        if result.timed_out:
            return False, f"`{argv}` did not finish within the step timeout"
        if not result.ok:
            if re.search(patterns["uncompilable"], blob):
                return False, (
                    f"`{argv}` exited {result.returncode} without compiling the "
                    f"tests: {result.tail(lines=4)}"
                )
            return True, (
                f"the `test` step exists and ran; it exited {result.returncode}, "
                f"which is not scored"
            )
        return True, f"`{argv}` ran in {result.duration:.1f}s"

    def check_rebuild_is_noop(self) -> tuple[bool, str]:
        """A second build must not redo the first one.

        Zig caches by content hash, so a correct build graph makes the second run
        near-instant.  A step with no declared output reruns every time, which is
        the defect this looks for.  The threshold is generous -- half the first
        build, or four seconds, whichever is larger -- because a scored check
        that fails costs the whole stage and must not turn on container noise.
        """
        observed = self.outcome.observed.get("rebuild")
        if not observed:
            return False, "no rebuild was attempted"
        if not observed.get("ok"):
            result = self.outcome.extra.get("rebuild")
            return False, (
                "the second build failed where the first succeeded"
                + (f": {result.tail(lines=3)}" if result else "")
            )
        first = float(observed.get("first_sec") or 0.0)
        second = float(observed.get("second_sec") or 0.0)
        budget = max(4.0, first * 0.5)
        if second > budget:
            return False, (
                f"the second `{self.build_argv('install')}` took {second:.1f}s "
                f"against {first:.1f}s for the first: a step is rebuilding "
                f"unconditionally"
            )
        return True, f"rebuild took {second:.1f}s after {first:.1f}s"

    def check_build_out_of_tree_cache(self) -> tuple[bool, str]:
        """The build must add nothing to the repository but its own output.

        This is about the submission staying the submission.  The harness collects
        `/workspace/repo` as it stands; a build that writes generated sources,
        downloaded files or a cache under a name of its own into the tree changes
        what gets graded, and in the worst case a generated `.go` file would fail a
        gate that has nothing to do with the port.

        The driver's own output names are expected and ignored -- `zig-out/` and the
        two cache names for a Zig build, `bin/` for a Go one -- and they are named
        from the driver rather than listed here so the allowance cannot be wider
        than what the build was told to produce.
        """
        allowed = ", ".join(build_lib.driver_outputs(self.driver)) or "its own output"
        added = self.outcome.observed.get("build_added_paths") or []
        if added:
            return False, (
                f"the build wrote {len(added)} path(s) into the repository beyond "
                f"{allowed}: {', '.join(added[:6])}"
            )
        stray = (self.outcome.observed.get("default_cache_build") or {}).get(
            "stray_paths") or []
        if stray:
            return False, (
                f"with the cache variables unset the build wrote {len(stray)} "
                f"path(s) outside {allowed}: {', '.join(stray[:6])}"
            )
        return True, "the build wrote nothing into the tree beyond its own output"

    # Deliberately absent: `manifest-shape`.  Whether build.zig sits at the root,
    # whether src/ is the source root, whether the manifest names itself -- those
    # are properties of a repository's layout, and this stage grades the behaviour
    # of a program.  They are stage-1 gates, judged by an agent that reads the
    # tree.  What this stage can say about the manifest, it already says: `zig
    # build` accepted it, or build/succeeds failed.

    def check_toolchain_pinned(self) -> tuple[bool, str]:
        """The compiler that built this must be the pinned one.

        Read through the same PATH the build saw, and from whichever compiler the
        resolved driver used: asking `zig version` after a Go build would record a
        real Zig version that had nothing to do with the binary.

        Exact match, not a floor.  0.14.1 is what the expectations were produced
        under and Zig's standard library changes shape between minor releases; a
        submission built with 0.15 is not the submission that was specified.

        The match is word-bounded rather than an equality, because the two
        toolchains report differently -- `zig version` prints `0.14.1` and nothing
        else, `go version` prints `go version go1.23.4 linux/amd64` -- and a
        substring test would accept 0.14.10 for 0.14.1.
        """
        want = (self.driver.get("toolchain_version")
                or self.state_b.get("zig_version")
                or self.policy.get("zig_toolchain") or "")
        observed = self.outcome.observed.get("compiler_version") or {}
        got = (observed.get("text")
               or self.outcome.observed.get("zig_version") or "").strip()
        argv = " ".join(observed.get("argv") or [f"{self.toolchain}", "version"])
        if not want:
            return False, (
                f"the contract pins no version for driver "
                f"{self.driver.get('id')!r}; there is nothing to check against"
            )
        if not got:
            result = (self.outcome.extra.get(f"{self.toolchain}_version")
                      or self.outcome.extra.get("zig_version"))
            return False, (
                f"`{argv}` produced nothing"
                + (f": {result.tail(lines=3)}" if result else "")
            )
        if not re.search(rf"(?<![\w.]){re.escape(want)}(?![\w.])", got):
            return False, (
                f"`{argv}` reports {got!r}, which does not name the pinned "
                f"{want!r}"
            )
        return True, f"built by {self.driver.get('language', self.toolchain)} {want}"

    # -- what the binary says about itself ---------------------------------

    def check_probe_handshake(self) -> tuple[bool, str]:
        """`hello` must match the reference byte for byte.

        Asked through the protocol, because the protocol is the library surface
        here and `yaml-probe --version` does not exist.  The comparison is against
        the contract's own declaration of the result, so this file holds no copy of
        the string.
        """
        if self.probe_answer is None:
            return False, "no probe session was available to ask"
        op = self.protocol.get("handshake_op") or "hello"
        response = self.probe_answer({"id": 1, "op": op, "source": ""})
        if response is None:
            return False, f"the probe did not answer a `{op}` request"
        if not response.get("ok"):
            return False, f"`{op}` returned an error: {str(response)[:200]}"
        result = response.get("result") or {}
        product = self.contract.get("product") or {}
        want = {
            "protocol": 1,
            "library": product.get("import_path"),
            "upstream": product.get("upstream_version"),
        }
        wrong = {k: (result.get(k), v) for k, v in want.items() if result.get(k) != v}
        if wrong:
            return False, "; ".join(
                f"{k}: got {got!r}, expected {exp!r}" for k, (got, exp) in wrong.items()
            )
        if list(result.keys()) != list(want.keys()):
            return False, (
                f"`{op}` result keys are {list(result.keys())}, expected "
                f"{list(want.keys())} in that order"
            )
        return True, f"`{op}` reports {result}"

    def check_probe_flushes(self) -> tuple[bool, str]:
        """One request, written alone, must be answered before more input arrives.

        The single most expensive defect a submission can ship.  A probe that
        buffers its stdout waits for a full buffer that never comes, the grader
        waits for a line that never comes, and the run dies on the session timeout
        rather than on one case -- so this is graded directly, with a short
        deadline, on a process of its own.

        Deliberately not folded into the protocol family: that family writes all
        its lines before reading any, which a buffering probe would survive.
        """
        if not self.artifact.is_file():
            return False, "there is no binary to ask"
        op = self.protocol.get("handshake_op") or "hello"
        line = json.dumps({"id": 1, "op": op, "source": ""},
                          separators=(",", ":")) + "\n"
        proc = subprocess.Popen(
            [str(self.artifact)], cwd=self.scratch, env=self.bare_env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            assert proc.stdin and proc.stdout
            proc.stdin.write(line.encode())
            proc.stdin.flush()
            # stdin stays open on purpose.  Closing it would let a fully buffered
            # probe pass: the flush at exit would deliver the response, and the
            # rule being graded is that a response arrives *before* EOF.
            sel = selectors.DefaultSelector()
            sel.register(proc.stdout, selectors.EVENT_READ)
            if not sel.select(timeout=30.0):
                return False, (
                    "no response within 30s while stdin stayed open: the probe "
                    "buffers its output"
                )
            sel.close()
            raw = vlib.read_ndjson_line(proc.stdout, limit=1 << 20)
            if not raw:
                return False, "the probe closed stdout without answering"
            try:
                payload = json.loads(raw)
            except ValueError:
                return False, f"the response is not JSON: {raw[:200]!r}"
            if payload.get("id") != 1:
                return False, f"answered id {payload.get('id')!r}, expected 1"
            return True, "answered one request without waiting for more input"
        finally:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)

    def check_probe_drains_stdin(self) -> tuple[bool, str]:
        """End of stdin means exit 0.

        A probe that exits nonzero at EOF, or that dies on SIGPIPE, makes every
        session end look like a crash -- and the case runner would then restart
        it repeatedly for no reason.
        """
        if not self.artifact.is_file():
            return False, "there is no binary to ask"
        result = vlib.run([str(self.artifact)], cwd=self.scratch,
                          env=self.bare_env(), timeout=PROBE_TIMEOUT,
                          stdin_data=b"")
        if result.timed_out:
            return False, "the probe did not exit on empty stdin"
        if result.returncode != 0:
            return False, (
                f"the probe exited {result.returncode} at end of stdin: "
                f"{result.tail(lines=3)}"
            )
        return True, "exits 0 at end of stdin"

    def check_probe_skips_blank_lines(self) -> tuple[bool, str]:
        """An empty line gets no response.

        Two requests with blank lines between and around them must produce exactly
        two responses.  A probe that answers a blank line puts every later
        response one position out of step with its request, and the whole run after
        that point is wrong for one reason.
        """
        if not self.artifact.is_file():
            return False, "there is no binary to ask"
        op = self.protocol.get("handshake_op") or "hello"
        req = json.dumps({"id": 7, "op": op, "source": ""}, separators=(",", ":"))
        stdin = f"\n{req}\n\n\n{req}\n\n".encode()
        result = vlib.run([str(self.artifact)], cwd=self.scratch,
                          env=self.bare_env(), timeout=PROBE_TIMEOUT,
                          stdin_data=stdin)
        if result.timed_out:
            return False, "the probe hung on input containing blank lines"
        lines = [ln for ln in result.stdout.split(b"\n") if ln.strip()]
        if len(lines) != 2:
            return False, (
                f"two requests among four blank lines produced {len(lines)} "
                f"response(s), expected 2"
            )
        return True, "blank lines produce no response"

    def check_probe_standalone(self) -> tuple[bool, str]:
        """The binary must still work with the build tree gone.

        The installed binary is copied out and asked every published op while the
        repository is renamed away, so the tree really is absent for the duration
        of the run rather than merely off the path the copy was started from.  A
        probe that opens a file under `src/` at run time, or that is a shell script
        pointing back into the tree, fails there.  The answers are compared against
        the same requests asked of the in-tree binary, because a process that
        starts and prints an error is not "still working".

        Deliberately not a byte search for the build directory.  Matching on
        `str(self.repo)` anywhere in the artefact is wrong for both toolchains:
        measured on zig 0.14.1, an honest `zig build` embeds its source root as a
        DWARF directory entry in Debug, ReleaseSafe and ReleaseFast -- three of
        the four optimisation modes, including the default -- and Go embeds one
        `.go` path per compiled file unless `-trimpath` is passed.  In neither
        case does the binary read anything from there; the string is debug
        metadata.  The rename asks the same question about behaviour instead.
        """
        observed = self.outcome.observed.get("standalone") or {}
        result = self.outcome.extra.get("standalone")
        if result is None or not observed.get("ran"):
            return False, (
                "the standalone probe was never run"
                + (f": {observed.get('reason')}" if observed.get("reason") else "")
            )
        where = ("with the repository renamed away"
                 if observed.get("tree_renamed_away")
                 else "from an unrelated directory (the tree could not be renamed "
                      "away, so this is the weaker form of the check)")
        if result.timed_out:
            return False, f"the copied binary did not answer within the timeout {where}"
        if not result.ok:
            return False, (
                f"the copied binary exited {result.returncode} {where}: "
                f"{result.tail(lines=3)}"
            )
        stdout = observed.get("stdout") or ""
        if '"ok"' not in stdout:
            return False, (
                f"the copied binary ran {where} but answered nothing usable: "
                f"{stdout[:200]!r}"
            )
        if not observed.get("answers_match_in_tree"):
            in_tree = self.outcome.extra.get("standalone_in_tree")
            return False, (
                f"the copied binary answered differently {where} than the same "
                f"binary answered inside the tree: it depends on something in the "
                f"build directory (in-tree exit "
                f"{observed.get('in_tree_returncode')}"
                + (f", {in_tree.returncode} outside" if in_tree else "")
                + ")"
            )
        return True, (
            f"answered {observed.get('requests')} requests identically {where}"
        )

    def check_no_runtime_deps(self) -> tuple[bool, str]:
        """NEEDED must hold nothing beyond libc and the platform's own libraries.

        "the Zig standard library only" is a claim about the source graph; this is
        the same claim checked on the delivered artefact, where a linked
        third-party library is what would actually show up.
        """
        path = self.artifact
        if not path.is_file() or not elflib.is_elf(path):
            return False, "there is no ELF binary to inspect"
        try:
            obj = elflib.load(path)
        except elflib.ElfError as exc:
            return False, f"unreadable ELF: {exc}"
        unexpected = [lib for lib in obj.needed if lib not in ALLOWED_NEEDED]
        if unexpected:
            return False, f"the binary needs {', '.join(sorted(unexpected))}"
        # RUNPATH is deliberately not graded here.  It belongs to
        # `probe-standalone`, and a binary with an rpath into the build tree has
        # made one mistake -- charging it to two cases would price that single
        # defect at both their weights.
        return True, (
            "shared library dependencies: "
            + (", ".join(sorted(obj.needed)) or "none (static)")
        )

    # Deliberately absent: `retained-paths`, `readme-updated`,
    # `no-build-leftovers`.  All three read the tree rather than the program, and
    # the middle one is the clearest case for why they belong to stage 1: deciding
    # whether a README "describes the Zig build" by searching it for the substring
    # `go get ` fails a submission that documents `zig build` in full and then
    # quotes the old command in a migration note.  An agent reading the file
    # answers the question that was actually asked; a regular expression answers a
    # different, cheaper one, and a submission can satisfy the cheaper one without
    # satisfying the real one.

    # -- driver -------------------------------------------------------------

    def evaluate(self, table) -> list[CaseOutcome]:
        """Run the cases in one declared table, in declaration order.

        `table` is `catalog.BUILD_CASES` or `catalog.PROBE_CASES`.  It is a
        parameter rather than a constant because the two are graded by different
        modules in different processes, and a module must run its own cases and
        no others: weight it did not run is weight it cannot be credited for.
        """
        outcomes: list[CaseOutcome] = []
        for case_id, check, weight, note in table:
            method = getattr(self, f"check_{check.replace('-', '_')}", None)
            if method is None:
                # A declared case with no implementation is a defect in this file,
                # reported as a failure rather than skipped: skipping would award
                # the weight of a check that never ran.
                outcomes.append(CaseOutcome(
                    case_id=case_id, family="behaviour", kind="probe",
                    passed=False, weight=weight,
                    detail=f"structure.py has no check for {check!r}",
                ))
                continue
            try:
                passed, detail = method()
            except Exception as exc:  # noqa: BLE001 - one bad check must not stop the phase
                passed, detail = False, f"{type(exc).__name__}: {exc}"[:400]
                self.log.write(f"  {case_id} raised: {exc}")
            outcomes.append(CaseOutcome(
                case_id=case_id, family="behaviour", kind="probe",
                passed=passed, weight=weight,
                detail=f"{note}: {detail}" if not passed else detail,
            ))
            self.log.write(f"  {'PASS' if passed else 'FAIL'} {case_id}: {detail[:160]}")
        scored = [o for o in outcomes if o.scored]
        earned = sum(o.weight for o in scored if o.passed)
        total = sum(o.weight for o in scored)
        line = (f"{sum(1 for o in scored if o.passed)}/{len(scored)} cases, "
                f"{earned:.1f}/{total:.1f} points")
        if len(scored) != len(outcomes):
            line += f" ({len(outcomes) - len(scored)} not scored on this driver)"
        self.log.write(line)
        return outcomes


def run_build_cases(
    repo: Path,
    outcome: BuildOutcome,
    contract: dict,
    scratch: Path,
    log: Log,
    *,
    probe_answer=None,
) -> list[CaseOutcome]:
    """The build and its artefact."""
    log.section("build and artefact")
    if len(catalog.BUILD_CASES) < catalog.MIN_BUILD_CASES:
        raise RuntimeError(
            f"{len(catalog.BUILD_CASES)} build cases declared; the catalog floor "
            f"is {catalog.MIN_BUILD_CASES}"
        )
    auditor = StructureAuditor(repo, outcome, contract, scratch, log,
                               probe_answer=probe_answer)
    return auditor.evaluate(catalog.BUILD_CASES)


def run_probe_cases(
    repo: Path,
    outcome: BuildOutcome,
    contract: dict,
    scratch: Path,
    log: Log,
    *,
    probe_answer=None,
) -> list[CaseOutcome]:
    """How the artefact behaves as a process."""
    log.section("process behaviour")
    if len(catalog.PROBE_CASES) < catalog.MIN_PROBE_CASES:
        raise RuntimeError(
            f"{len(catalog.PROBE_CASES)} probe cases declared; the catalog floor "
            f"is {catalog.MIN_PROBE_CASES}"
        )
    auditor = StructureAuditor(repo, outcome, contract, scratch, log,
                               probe_answer=probe_answer)
    return auditor.evaluate(catalog.PROBE_CASES)
