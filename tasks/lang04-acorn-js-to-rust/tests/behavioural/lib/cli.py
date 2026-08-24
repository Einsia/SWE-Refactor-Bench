#!/usr/bin/env python3
"""Runs the `acorn` command-line differential: submission against the reference.

The CLI is a smaller surface than the library but a stricter one, because all
three of its channels are graded: stdout, stderr and the exit status.  A port
that gets the AST right and the exit code wrong has not reproduced `acorn`, and
a downstream `acorn file.js || echo bad` would notice before any AST consumer
did.

Every case is run twice in the same container -- once against the pinned
JavaScript at /opt/swerefactor/reference, once against the submission's installed
binary -- with the same argv, the same stdin, the same cwd and the same
environment.  Nothing is compared against a value recorded at image build time,
because the CLI's output contains filenames: the reference must see the same
arguments the submission does, and only a same-run pairing guarantees that.

No normalisation is applied to either side.  The output is compared as the two
programs produced it, which is possible because the case sources are written
into a scratch directory that is also the cwd, and are named on argv by their
basename -- so the filename State A splices into an error message is `es2015.js`
and nothing in the expected bytes depends on where the run happened.

Four argv shapes cannot be graded on bytes, and are declared `status` in the
catalog rather than quietly excused here: they reach node's `readFileSync` on an
unreadable path, State A lets the error escape, and node prints a stack trace
naming absolute paths inside its own `dist/bin.js`.  Those bytes belong to node.
For those cases the exit status is compared, stdout must be empty on both sides,
and both must have written something to stderr.  instruction.md says so, so a
port knows in advance which four they are and what is expected of them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import catalog
import vlib
from vlib import CaseOutcome, Log, Result

# A CLI case is one parse of a source under 100 lines.  The reference itself
# answers in ~90 ms including node startup; a native binary that needs a minute
# is not going to be right about anything either, and the ceiling keeps a
# hanging binary from eating the verifier's budget 24 times over.
CASE_TIMEOUT = 60.0

# stdout for `--locations` on the module source is ~40 KB.  The cap exists only
# to bound a submission that prints an infinite tree; it is far above every
# legitimate response, and a case that hits it is reported as a failure with the
# truncation named rather than silently compared as a prefix.
MAX_OUTPUT = 8 * 1024 * 1024


@dataclass
class CliCase:
    """One resolved case: argv with `<file:KEY>` replaced by a basename."""

    case_id: str
    argv: list[str]
    stdin: bytes | None
    weight: float
    mode: str
    note: str


class CliRunner:
    """Runs every declared CLI case against both implementations."""

    def __init__(
        self,
        submission_bin: Path,
        reference_dir: Path,
        scratch: Path,
        log: Log,
        *,
        env: dict | None = None,
        node_bin: Path | str | None = None,
    ) -> None:
        self.submission_bin = submission_bin
        self.reference_dir = reference_dir
        self.scratch = scratch
        self.log = log
        self.env = env or vlib.base_env()
        # The interpreter, by absolute path.  Both sides are run with the same
        # environment, and during grading that environment's PATH resolves `node`
        # to a tripwire -- deliberately, so a submission cannot reach an
        # interpreter while its CLI is being measured.  The reference has to run
        # anyway, so it is invoked by path rather than by name.
        self.node_bin = str(node_bin) if node_bin else "node"
        # Sources live here.  Both implementations are handed paths into this
        # directory, and it is also the cwd for every case, so a relative path in
        # an error message resolves the same way for both.
        self.case_dir = scratch / "cli-cases"
        self.sources: dict[str, Path] = {}
        # Cases whose expected bytes turned out to be unreproducible.  See
        # `audit_expectation`.  A non-empty list is a defect in the case
        # declarations, and the phase is withheld rather than scored.
        self.unportable: list[str] = []

    # -- setup ------------------------------------------------------------

    def write_sources(self) -> dict[str, Path]:
        """Materialise CLI_SOURCES on disk, byte for byte as declared.

        Written once and reused across both implementations rather than once per
        side: a case where the two saw different bytes would be a verifier bug
        that looks exactly like a submission bug.
        """
        if self.case_dir.exists():
            shutil.rmtree(self.case_dir, ignore_errors=True)
        self.case_dir.mkdir(parents=True, exist_ok=True)
        for key, text in sorted(catalog.CLI_SOURCES.items()):
            path = self.case_dir / f"{key}.js"
            path.write_text(text, encoding="utf-8", newline="")
            self.sources[key] = path
        for name in catalog.CLI_DIRS:
            (self.case_dir / name).mkdir(exist_ok=True)
        self.log.write(
            f"cli: wrote {len(self.sources)} sources and "
            f"{len(catalog.CLI_DIRS)} directories to {self.case_dir}"
        )
        return dict(self.sources)

    def resolve(self) -> list[CliCase]:
        """Turn catalog.CLI_CASES into runnable cases.

        `<file:KEY>` becomes a bare basename, not a path.  State A splices the
        argument as written into the `(line:column)` suffix of a parse error, so
        an absolute path would put the run's scratch directory into the expected
        bytes and make every error case dependent on where the verifier ran.  The
        cwd is the case directory, so a basename resolves.
        """
        out: list[CliCase] = []
        for case_id, argv, stdin_key, weight, mode, note in catalog.CLI_CASES:
            resolved: list[str] = []
            for arg in argv:
                match = re.fullmatch(r"<file:([a-z0-9-]+)>", arg)
                if match:
                    key = match.group(1)
                    if key not in self.sources:
                        raise KeyError(
                            f"{case_id} refers to <file:{key}>, which is not in "
                            f"catalog.CLI_SOURCES"
                        )
                    resolved.append(self.sources[key].name)
                else:
                    resolved.append(arg)
            stdin = None
            if stdin_key is not None:
                if stdin_key not in catalog.CLI_SOURCES:
                    raise KeyError(
                        f"{case_id} reads stdin key {stdin_key!r}, which is not "
                        f"in catalog.CLI_SOURCES"
                    )
                stdin = catalog.CLI_SOURCES[stdin_key].encode("utf-8")
            if mode not in catalog.CLI_MODES:
                raise ValueError(
                    f"{case_id} declares grading mode {mode!r}, not one of "
                    f"{catalog.CLI_MODES}"
                )
            out.append(CliCase(case_id, resolved, stdin, weight, mode, note))
        return out

    # -- execution --------------------------------------------------------

    def _run_reference(self, case: CliCase) -> Result:
        """The pinned JavaScript CLI, through node.

        `bin/acorn` is invoked as a script rather than through the package's
        `bin` entry, because npm's generated shim adds its own error handling and
        the graded artefact is acorn's CLI, not npm's wrapper.
        """
        argv = [self.node_bin, str(self.reference_dir / "acorn/bin/acorn"),
                *case.argv]
        return vlib.run(
            argv,
            cwd=self.case_dir,
            env=self.env,
            timeout=CASE_TIMEOUT,
            stdin_data=case.stdin,
            log=None,
            full_capture=True,
        )

    def _run_submission(self, case: CliCase) -> Result:
        argv = [str(self.submission_bin), *case.argv]
        return vlib.run(
            argv,
            cwd=self.case_dir,
            env=self.env,
            timeout=CASE_TIMEOUT,
            stdin_data=case.stdin,
            log=None,
            full_capture=True,
        )

    def audit_expectation(self, case: CliCase, want: Result) -> str | None:
        """Reject an expectation only the reference could ever produce.

        A `bytes` case is graded on the reference's exact output, so that output
        has to be something a Rust program could also emit.  Two things
        disqualify it, and both are things node adds rather than acorn:

          a stack frame        `    at Object.<anonymous> (...)`.  It appears
                               whenever an exception escapes the CLI, which
                               happens for an unreadable file and -- less
                               obviously -- for a BigInt in the tree, because
                               `JSON.stringify` throws on one.
          the reference's path node names the script it was running.  That path
                               is inside the verifier image.

        Without this check the trap is invisible: run the reference against
        itself and such a case passes, because both sides are the same file at
        the same path.  It only breaks later, against a real submission, and
        then it looks like the submission's fault.  Cases like this are legal --
        `status` mode exists for them -- but they have to be declared, and this
        is what makes declaring them mandatory.
        """
        if case.mode != "bytes":
            return None
        blob = want.stdout + want.stderr
        if re.search(rb"^\s+at\s+\S+.*\(.*\)\s*$", blob, re.MULTILINE):
            return (f"{case.case_id} is declared `bytes`, but the reference's "
                    f"output contains a node stack frame; no port can reproduce "
                    f"it, so the case must be `status`")
        ref_root = str(self.reference_dir).encode()
        if ref_root in blob:
            return (f"{case.case_id} is declared `bytes`, but the reference's "
                    f"output names its own path ({self.reference_dir}); that is "
                    f"not reproducible, so the case must be `status`")
        return None

    def check_reference(self, cases: list[CliCase]) -> list[str]:
        """Confirm the reference runs at all before grading anything against it.

        If node is missing or the reference tree is incomplete, every case fails
        identically and the report would read as a catastrophic submission
        failure.  It is not: it is a broken image, and it must be reported as
        one.
        """
        probe = CliCase("cli/probe", ["--help"], None, 0.0, "bytes",
                        "reference check")
        result = self._run_reference(probe)
        # `--help` exits 0 and prints the usage banner to stdout.  There is no
        # `--version`: the CLI has no such flag, and asking for one is how a
        # runner written from the usage string instead of from the program
        # discovers that the usage string is not the program.
        if not result.ok or b"usage: acorn" not in result.stdout:
            detail = result.tail() or "no output"
            self.log.write(f"cli: REFERENCE UNUSABLE: {detail[:300]}")
            return [f"reference `acorn --help` failed: {detail[:300]}"]
        self.log.write(f"cli: reference responds to --help ({len(cases)} cases queued)")
        return []

    def run(self, cases: list[CliCase]) -> list[CaseOutcome]:
        """Grade every case, comparing all three channels."""
        outcomes: list[CaseOutcome] = []
        if not self.submission_bin.is_file():
            self.log.write(
                f"cli: no acorn binary at {self.submission_bin}; every case fails"
            )
            return [
                CaseOutcome(
                    case_id=case.case_id, family="cli", kind="cli",
                    passed=False, weight=case.weight,
                    detail=f"no executable at {self.submission_bin}; "
                           f"`make install` was supposed to put one there",
                )
                for case in cases
            ]

        for case in cases:
            want = self._run_reference(case)
            unportable = self.audit_expectation(case, want)
            if unportable:
                # A verifier defect, not a submission one.  It is recorded on the
                # case so the report shows which case and why, and it is counted
                # as a failure of the verifier rather than scored against the
                # submission -- run_cli_phase turns any of these into an image
                # problem, which withholds the phase instead of grading it.
                self.unportable.append(unportable)
                self.log.write(f"cli: UNPORTABLE EXPECTATION: {unportable}")
                continue
            got = self._run_submission(case)
            outcomes.append(self._compare(case, want, got))

        passed = sum(1 for o in outcomes if o.passed)
        self.log.write(f"cli: {passed}/{len(outcomes)} cases match the reference")
        return outcomes

    def _compare(self, case: CliCase, want: Result, got: Result) -> CaseOutcome:
        """One case's verdict over stdout, stderr and exit status."""
        duration = got.duration
        if want.timed_out:
            # The reference timing out is an image problem, not a submission one.
            return CaseOutcome(
                case_id=case.case_id, family="cli", kind="cli",
                passed=False, weight=case.weight, duration=duration,
                detail=f"reference timed out after {CASE_TIMEOUT:.0f}s; "
                       f"this case could not be graded",
            )
        if got.timed_out:
            return CaseOutcome(
                case_id=case.case_id, family="cli", kind="cli",
                passed=False, weight=case.weight, duration=duration,
                detail=f"{case.note}: submission did not exit within "
                       f"{CASE_TIMEOUT:.0f}s (argv: {_argv_text(case.argv)})",
            )
        if got.returncode == -1 and not got.stdout and b"No such file" in got.stderr:
            return CaseOutcome(
                case_id=case.case_id, family="cli", kind="cli",
                passed=False, weight=case.weight, duration=duration,
                detail=f"could not execute the submission's binary: "
                       f"{got.stderr.decode('utf-8', 'replace')[:200]}",
            )

        problems: list[str] = []
        if len(got.stdout) >= MAX_OUTPUT:
            problems.append(
                f"stdout hit the {MAX_OUTPUT // (1024 * 1024)} MB cap; the "
                f"reference produced {len(want.stdout)} bytes"
            )

        # Exit status is compared as the number, not as zero/non-zero, in both
        # modes.  acorn exits 1 on every failure it reports itself, and a port
        # that exits 2 because that is what its argument parser does has changed
        # the interface for anything that inspects `$?`.
        if want.returncode != got.returncode:
            problems.append(
                f"exit status {got.returncode}, reference {want.returncode}"
            )

        if case.mode == "bytes":
            if want.stdout != got.stdout:
                problems.append("stdout differs")
            if want.stderr != got.stderr:
                problems.append("stderr differs")
        else:
            # `status`: the streams are checked for shape, not content.  See the
            # module docstring for why these four cases cannot be graded on bytes.
            if got.stdout:
                problems.append(
                    f"wrote {len(got.stdout)} bytes to stdout; the reference "
                    f"fails this case before printing anything"
                )
            if not got.stderr.strip():
                problems.append(
                    "wrote nothing to stderr; a failure has to say something"
                )

        if not problems:
            return CaseOutcome(
                case_id=case.case_id, family="cli", kind="cli",
                passed=True, weight=case.weight, duration=duration,
                detail="" if case.mode == "bytes" else
                       "exit status and stream use only (see instruction.md)",
            )
        return CaseOutcome(
            case_id=case.case_id, family="cli", kind="cli",
            passed=False, weight=case.weight, duration=duration,
            detail=f"{case.note}: {', '.join(problems)} "
                   f"(argv: {_argv_text(case.argv)}"
                   f"{', stdin' if case.stdin else ''})",
            diff=self._diff(want.stdout, got.stdout, want.stderr, got.stderr)
                 if case.mode == "bytes" else "",
        )

    def _diff(self, want_out: bytes, got_out: bytes,
              want_err: bytes, got_err: bytes) -> str:
        """The first channel that differs, shown at the point it differs."""
        parts: list[str] = []
        if want_out != got_out:
            offset, ctx = vlib.first_difference(want_out, got_out)
            parts.append(f"stdout {ctx}" if ctx else "stdout differs")
        if want_err != got_err:
            # stderr is short enough to show whole, and an error message read
            # end to end is more useful than a byte offset into it.
            parts.append(
                "stderr:\n"
                f"  reference  {want_err.decode('utf-8', 'replace').strip()[:500]!r}\n"
                f"  submission {got_err.decode('utf-8', 'replace').strip()[:500]!r}"
            )
        return "\n".join(parts)[:2000]


def _argv_text(argv: list[str]) -> str:
    """argv for the report, with the empty string made visible."""
    return " ".join(arg if arg else "''" for arg in argv)


def run_cli_phase(
    submission_prefix: Path,
    reference_dir: Path,
    scratch: Path,
    log: Log,
    *,
    env: dict | None = None,
    node_bin: Path | str | None = None,
) -> tuple[list[CaseOutcome], list[str]]:
    """The whole CLI phase: write sources, check the reference, grade.

    Returns (outcomes, image_problems).  A non-empty second element means the
    reference could not be run and the outcomes are not a measurement of the
    submission -- the caller must report that rather than the score.
    """
    log.section("cli differential")
    runner = CliRunner(
        submission_prefix / "bin/acorn", reference_dir, scratch, log, env=env,
        node_bin=node_bin,
    )
    runner.write_sources()
    cases = runner.resolve()
    if len(cases) < catalog.MIN_CLI_CASES:
        raise RuntimeError(
            f"only {len(cases)} CLI cases resolved; catalog declares a floor of "
            f"{catalog.MIN_CLI_CASES}"
        )
    problems = runner.check_reference(cases)
    if problems:
        return [], problems
    outcomes = runner.run(cases)
    if runner.unportable:
        # Grading the rest would report a partial score as if it were the whole
        # phase.  The caller has to know the phase is unsound.
        return outcomes, runner.unportable
    return outcomes, []


def main() -> int:
    """Run the CLI differential standalone, for developing the task itself.

    usage: cli.py PREFIX REFERENCE_DIR [SCRATCH]
    """
    import sys

    if len(sys.argv) < 3:
        print(main.__doc__)
        return 2
    prefix = Path(sys.argv[1]).resolve()
    reference = Path(sys.argv[2]).resolve()
    scratch = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else Path("/tmp/cliws")
    scratch.mkdir(parents=True, exist_ok=True)
    log = Log(scratch / "cli.log")
    env = vlib.base_env(PATH=os.environ.get("PATH", "/usr/bin:/bin"))
    outcomes, problems = run_cli_phase(prefix, reference, scratch, log, env=env)
    for problem in problems:
        print("IMAGE:", problem)
    for outcome in outcomes:
        print(("PASS " if outcome.passed else "FAIL "), outcome.case_id)
        if not outcome.passed:
            print("      ", outcome.detail[:300])
            if outcome.diff:
                for line in outcome.diff.splitlines()[:8]:
                    print("       ", line[:200])
    total = sum(o.weight for o in outcomes)
    earned = sum(o.weight for o in outcomes if o.passed)
    print(f"\n{sum(1 for o in outcomes if o.passed)}/{len(outcomes)} cases, "
          f"{earned:.1f}/{total:.1f} points")
    log.close()
    return 0 if outcomes and all(o.passed for o in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
