"""The command-line surface, compared as behaviour rather than as text.

State A's jar is a Dropwizard application, so its argv[0] selects a command:
`server`, `check`, `import`, `match`.  That dispatch is a documented part of the
application's interface — the import command is how you build a graph cache
without starting a server, and it is what every deployment guide tells you to
run first.  A rewrite that keeps the HTTP surface and loses the CLI has dropped
a feature its users have in their shell history.

What is graded here is deliberately narrow, and the narrowness is the point:

  * the EXIT CODE for each invocation, and
  * whether the invocation had an EFFECT, where the effect is observable outside
    the process (a graph cache directory appearing on disk).

What is NOT graded: the text of usage messages, the wording of errors, the order
of a subcommand listing, the presence of a banner.  Every framework's argument
parser writes those differently, they are not a contract any script can depend
on, and grading them would be grading the framework's identity rather than the
application's behaviour — which is the mistake this whole suite is built to
avoid.  An exit code is what a shell script branches on; the prose above it is
not.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from swerefactor.contract import submission_env


class Invocation:
    def __init__(self, cid: str, args: list[str], why: str, *,
                 expect_effect: str | None = None, timeout: float = 900.0):
        self.id = cid
        self.args = args
        self.why = why
        # A path, relative to the working directory, that the invocation should
        # create if it does what it says.  Compared as present/absent on both
        # sides, never by content: two graph caches built by two builds of the
        # same code are not byte-identical, and comparing them would fail
        # everyone.
        self.expect_effect = expect_effect
        self.timeout = timeout


def invocations(config_name: str) -> list[Invocation]:
    """The graded CLI cases.

    `{config}` in an argument list is substituted with the config path at run
    time, so the same table works for both sides in different directories.
    """
    return [
        Invocation(
            "cli-no-args", [],
            "the jar with no arguments at all: a launcher that dispatches on "
            "argv reports an error and exits non-zero, and one that ignores "
            "argv starts a server and hangs.  The exit code separates them"),
        Invocation(
            "cli-unknown-command", ["definitely-not-a-command"],
            "an unrecognised command: must be refused, not treated as a config "
            "file or silently ignored"),
        Invocation(
            "cli-check", ["check", "{config}"],
            "`check` validates the configuration and exits without binding a "
            "port — the command a deployment pipeline runs before rolling out, "
            "and the cheapest proof that config parsing is reachable from the "
            "CLI"),
        Invocation(
            "cli-check-missing-config", ["check", "no-such-file.yml"],
            "`check` against a config that does not exist: a non-zero exit, "
            "where a rewrite that ignores its argument would exit 0 and report "
            "a valid configuration for a file it never read"),
        Invocation(
            "cli-check-malformed-config", ["check", "malformed.yml"],
            "`check` against a file that exists and is not valid YAML: the "
            "failure has to come from parsing rather than from absence"),
        Invocation(
            "cli-import", ["import", "{config}"],
            "`import` builds the graph cache and exits — the documented way to "
            "prepare a deployment without serving traffic.  Graded on exit code "
            "and on whether the cache directory appears, never on its bytes",
            expect_effect="graph-cache-cli", timeout=1800.0),
    ]


class Result:
    def __init__(self, code: int, effect: bool | None, stdout_bytes: int,
                 log: Path):
        self.code = code
        self.effect = effect
        self.stdout_bytes = stdout_bytes
        self.log = log


def run(jar: Path, inv: Invocation, workdir: Path, config: Path,
        log_dir: Path) -> Result:
    """Run one invocation in an isolated working directory."""
    run_dir = workdir / inv.id
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    # The config is copied in, so the `import` command's graph-cache path is
    # relative to a directory this invocation owns and two invocations cannot
    # observe each other's output.
    local_config = run_dir / config.name
    local_config.write_bytes(config.read_bytes())
    (run_dir / "malformed.yml").write_bytes(
        b"graphhopper:\n  this is: [not, valid\n    yaml at all\n")

    args = [a.format(config=local_config.name) for a in inv.args]
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{inv.id}.log"

    # The submission's own jar runs here, so it gets `submission_env` rather than
    # a copy of this process's environment: the SRB_* contract names would name
    # the tests directory and the result file to the thing being graded.
    env = submission_env()
    env["JAVA_TOOL_OPTIONS"] = ("-Xmx1500m -Duser.timezone=UTC "
                                "-Dfile.encoding=UTF-8")
    with open(log_path, "wb") as sink:
        try:
            proc = subprocess.run(
                ["java", "-jar", str(jar)] + args,
                cwd=str(run_dir), stdout=sink, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=env, timeout=inv.timeout,
                check=False)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            # A timeout is a distinct outcome, not an error to raise: an
            # invocation that should exit and instead serves forever is exactly
            # what `cli-no-args` is looking for, and it has to be comparable
            # between the two sides.
            code = -1
    effect = None
    if inv.expect_effect:
        target = run_dir / inv.expect_effect
        effect = target.is_dir() and any(target.iterdir())
    return Result(code, effect, log_path.stat().st_size, log_path)


def compare(inv: Invocation, ref: Result, sub: Result) -> str:
    """Raise AssertionError on a graded difference, else return a note."""
    problems = []
    if ref.code != sub.code:
        ref_desc = "timed out" if ref.code == -1 else f"exited {ref.code}"
        sub_desc = "timed out" if sub.code == -1 else f"exited {sub.code}"
        problems.append(f"exit status: reference {ref_desc}, submission {sub_desc}")
    if inv.expect_effect and ref.effect != sub.effect:
        problems.append(
            f"effect on disk ({inv.expect_effect}): reference "
            f"{'produced it' if ref.effect else 'did not'}, submission "
            f"{'produced it' if sub.effect else 'did not'}")
    if problems:
        raise AssertionError(
            f"{inv.id}: {len(problems)} difference(s)\n"
            f"  what this case grades: {inv.why}\n  "
            + "\n  ".join(problems)
            + f"\n  reference log: {ref.log}\n  submission log: {sub.log}\n"
            f"  (only exit status and on-disk effect are graded; message "
            f"wording is not)")
    detail = "timed out on both sides" if ref.code == -1 else f"both exited {ref.code}"
    if inv.expect_effect:
        detail += f", both {'produced' if ref.effect else 'did not produce'} " \
                  f"{inv.expect_effect}"
    return f"{inv.id}: {detail}"
