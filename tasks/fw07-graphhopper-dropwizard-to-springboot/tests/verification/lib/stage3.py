"""Build one tree, keep two servers up on it, run one candidate against them.

Called by run-candidate.sh, once per (candidate, tree).  Everything expensive is
cached under `$SRB_WORK/<token>`, which CandidateRunner shares across candidates
AND across all six rounds, so the costs below are paid once per tree per stage
rather than once per candidate:

  the Maven build     minutes, offline, the same command stage 2 uses
  the graph import    GraphHopper reads the OSM extract and runs the CH and LM
                      preparations on first boot and writes them to disk; a later
                      boot loads what is there
  the launch ladder    up to six invocations before one binds a port

Servers are kept ALIVE between candidates, which fw06's equivalent deliberately
does not do.  The difference is what the two applications hold: fw06's server
writes uploads into a document root, so a shared instance would make one
candidate's result depend on which candidate ran before it.  GraphHopper serves no
endpoint that mutates anything -- /route, /isochrone, /spt, /match, /nearest,
/mvt and the transit pair are all readers, and the graph is opened once at startup
-- so a second candidate asks the same server the same questions and gets the same
answers.  Liveness is re-checked over HTTP on every invocation and a dead server is
replaced, so a candidate that manages to kill one costs a reboot rather than a
wrong verdict.

WHAT THE CANDIDATE IS TOLD, AND WHAT IT IS NOT.  It gets host, ports and profile
names, inline in one environment variable.  It does not get the tree path, the
token, the work root, or a path to any file this module wrote -- and that is a
deliberate tightening over the obvious design.  Handing over `$WORK/<token>/
servers.json` would let a candidate walk one directory up and read `build.log`,
which names the framework that was compiled and therefore answers "am I on the
original?" without asking either server anything.  probe.toml's scope rejects such
a candidate on sight, but a rule enforced only by an adjudicator's judgement is
enforced late, and this one is nearly free to enforce mechanically.

Exit codes are the contract with run-candidate.sh:

  0        the candidate passed against this tree
  1-5      pytest's own failure codes: the candidate failed
  71       this tree could not be built              -- an infrastructure fault
  74       this tree could not be booted             -- an infrastructure fault
  70       this module itself broke

A fault is not a verdict.  See fault() for why the distinction is worth the code.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import maven, server, wire   # noqa: E402

DATA = Path(os.environ.get("SRB_ADV_DATA", "/tests/verification/data"))

#: The two graph profiles, and the config template each is launched with.  Both,
#: not one: under `config-base.yml` the transit endpoints are not registered at
#: all, so a probe confined to it could not reach /route-pt, /isochrone-pt or
#: /pt-mvt -- and the transit wiring, where three PtRouter implementations compete
#: for one injection point, is among the likeliest places for a port to be wrong.
#: These are stage 2's own `corpus.PROFILES`, and the config files are byte copies.
PROFILES = {"base": "config-base.yml", "pt": "config-pt.yml"}

#: Stage 2's own launch timeout, unchanged.  A cold boot imports the extract and
#: runs both preparations; a timeout that fitted a warm boot would fail both sides.
READY_TIMEOUT = float(os.environ.get("SRB_ADV_READY_TIMEOUT", "420"))
BUILD_TIMEOUT = float(os.environ.get("SRB_ADV_BUILD_TIMEOUT", "2400"))
#: Per-candidate, inside pytest.  A candidate that hangs on a read must not eat
#: the round's whole budget.
TEST_TIMEOUT = os.environ.get("SRB_ADV_TEST_TIMEOUT", "150")


class Fault(RuntimeError):
    """This tree cannot be built or booted, so no candidate can be run on it."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def log(msg: str) -> None:
    print(f"[{TOKEN}] {msg}", file=sys.stderr, flush=True)


def fault(code: int, message: str) -> None:
    """Record an infrastructure fault where a reviewer will find it.

    Marked rather than converted into a verdict, and the reason is worth being
    precise about.  The adjudicator's only signal is the exit status, and it treats
    "passed on the original, failed on the submission" as a discovered defect.  A
    fault on the submission side is therefore indistinguishable from a real
    divergence, and a PERSISTENT one reproduces across all three reruns and is
    upheld -- ten points taken off a submission for a defect it does not have.
    The obvious fix, failing both roles, inverts the error into six survivals and
    pays the full 30 for nothing.  Both directions are wrong, and neither can be
    chosen from inside this process, which sees one tree.

    So the fault is made loud instead: a fixed prefix on stderr, which the
    adjudicator records verbatim in the round transcript, and a file under
    $SRB_WORK, which outlives the round.  score.py prints a warning naming every
    one, above the numbers it might have distorted, and does not change the score.
    Nothing here reaches the candidate: stderr is the harness's, not the test's.
    """
    log(f"SRB-INFRA-FAULT: {message}")
    try:
        faults = WORK / "faults"
        faults.mkdir(parents=True, exist_ok=True)
        (faults / f"{TOKEN}.{os.getpid()}").write_text(
            f"{TOKEN}\texit {code}\t{message}\n")
    except OSError:
        pass


TARGET = Path(os.environ["SRB_TARGET"])
TOKEN = os.environ["SRB_TARGET_TOKEN"]
WORK = Path(os.environ.get("SRB_WORK", "/tmp/swerefactor-verification/run"))
#: Keyed by the token and not by the role: nothing this process writes may be
#: named in a way that answers which tree it is.
STATE = WORK / TOKEN


# --------------------------------------------------------------------------- #
# 1. The build
# --------------------------------------------------------------------------- #
def build_tree() -> Path:
    """Build the tree if it has not been built, and return its jar.

    Cached two ways, and the negative cache matters as much as the positive one.
    `.jar` records a COMPLETED build -- a jar that exists because an interrupted
    build left one must not be reused -- and `.buildfail` records a build that was
    attempted and failed.  Without the second, a submission that does not compile
    would pay the full Maven timeout on every one of the ~120 executions a round
    can contain, and the round would end having attacked nothing.

    A cached failure is a decision with a cost: a transient failure (a full disk)
    poisons the tree for the rest of the stage.  Accepted, because the reason is
    written down next to the marker and a fault is already recorded, so a reviewer
    reading a 0/60 finds "the tree did not build, here is the log" rather than a
    silently generous score.
    """
    jarfile = STATE / ".jar"
    failed = STATE / ".buildfail"
    if failed.is_file():
        raise Fault(71, f"this tree failed to build earlier: "
                        f"{failed.read_text().strip()[:400]}")
    if jarfile.is_file():
        jar = Path(jarfile.read_text().strip())
        if jar.is_file():
            return jar
        # The marker outlived the artefact it names.  Rebuild rather than trust it.
        jarfile.unlink(missing_ok=True)

    log("building the tree (paid once per tree for the whole stage)")
    buildlog = STATE / "build.log"

    # Both trees, before either is built, exactly as stage 2 does it -- the same
    # function, from the same file.  A jar the agent committed into `web/target/`
    # would otherwise be discovered by find_app_jar and served in place of the
    # sources, and a stage-3 divergence found against it would be a divergence
    # between two jars neither of which came from the tree it was attributed to.
    removed = maven.scrub_build_output(TARGET)
    if removed:
        log(f"removed pre-existing build output: {', '.join(removed[:8])}"
            + (" ..." if len(removed) > 8 else ""))

    # Also both trees, before either is built, and for the same reason: a tree
    # carrying epoch-0 mtimes builds green and packages a jar with none of its
    # unfiltered resources in it.  See maven.normalise_mtimes -- State A arrives
    # that way, from a tarball whose timestamps are fixed so its bytes are
    # reproducible, and the resource it loses is the one it needs to boot.
    zeroed = maven.normalise_mtimes(TARGET)
    if zeroed:
        log(f"re-stamped {zeroed} path(s) that carried mtime 0 "
            f"(unfiltered resources would not have been copied)")

    try:
        maven.build(TARGET, buildlog, goals=["package"], timeout=BUILD_TIMEOUT)
    except maven.BuildFailed as exc:
        interesting = [l for l in exc.log.splitlines()
                       if any(k in l for k in ("ERROR", "BUILD FAILURE",
                                               "cannot find symbol",
                                               "Could not resolve",
                                               "Non-resolvable"))]
        failed.write_text("\n".join(interesting[:40]) or str(exc))
        raise Fault(71, f"the tree does not build; see {buildlog}") from exc
    except subprocess.TimeoutExpired as exc:
        failed.write_text(f"the build did not finish within {BUILD_TIMEOUT:g}s")
        raise Fault(71, f"the build timed out; see {buildlog}") from exc

    try:
        jar, notes = maven.find_app_jar(TARGET)
    except RuntimeError as exc:
        failed.write_text(str(exc))
        raise Fault(71, "the build succeeded but produced no jar `java -jar` "
                        f"could start; see {buildlog}") from exc
    (STATE / "jar-notes.txt").write_text("\n".join(notes))
    jarfile.write_text(str(jar))
    log(f"built: {jar.relative_to(TARGET)} "
        f"({jar.stat().st_size // (1024 * 1024)} MiB)")
    return jar


# --------------------------------------------------------------------------- #
# 2. The servers
# --------------------------------------------------------------------------- #
def free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def write_config(profile: str, app_port: int, admin_port: int) -> Path:
    """Materialise a profile's config for this tree, with these ports.

    The template comes from data/, byte-identical to stage 2's, and NOT from the
    tree: a submission is free to rewrite its own config-example.yml, and a grader
    that read the tree's copy would be handing one side a different question.

    The graph goes under $STATE rather than under the tree's `target/`, which is
    where stage 2 puts it.  Same directory contents, different place, for a reason
    local to this stage: build_tree() removes every `target/` before building, and
    a graph living there would be imported again after any rebuild.  Nothing about
    the question changes -- the path appears only as a value in the config.
    """
    template = (DATA / "configs" / PROFILES[profile]).read_text()
    graph = STATE / f"graph-{profile}"
    text = (template
            .replace("@APP_PORT@", str(app_port))
            .replace("@ADMIN_PORT@", str(admin_port))
            .replace("@GRAPH_DIR@", str(graph))
            .replace("@REPO_ROOT@", str(TARGET)))
    out = STATE / f"srb-{profile}.yml"
    out.write_text(text)
    return out


def responding(port: int, path: str = "/info", timeout: float = 8.0) -> bool:
    """Does something answer HTTP on this port?

    Any status counts, 404 and 500 included.  This is a liveness probe, not a
    correctness one: what /info ANSWERS is graded, by candidates, and a probe that
    demanded a 200 would turn a submission whose /info regressed into a boot fault
    -- which is the one failure mode this stage must not manufacture, because a
    fault on the original side pays the submission all six rounds.
    """
    try:
        wire.Transport("127.0.0.1", port, timeout=timeout).send("GET", path, {},
                                                               None)
        return True
    except Exception:
        return False


def kill_group(pid: int) -> None:
    """TERM then KILL a recorded process group, best effort.

    The group and not the process: a submission is free to start its jar through
    a wrapper, and a JVM parented elsewhere would keep its port and make the next
    boot fail to bind for a reason that has nothing to do with the code.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        for _ in range(60 if sig == signal.SIGTERM else 30):
            try:
                os.killpg(pid, 0)
            except OSError:
                return
            time.sleep(0.5)


def usable(record: dict) -> bool:
    """Is a recorded server still the running server it claims to be?

    Two questions, and both are needed.  `killpg(pid, 0)` proves the process
    group still exists; the HTTP probe proves it is the one holding the port,
    because a pid can be recycled and a JVM can be alive with its connector shut.
    """
    pid = record.get("pid")
    if not isinstance(pid, int):
        return False
    try:
        os.killpg(pid, 0)
    except OSError:
        return False
    return responding(int(record["app_port"]))


def boot(profile: str, jar: Path) -> dict:
    """Start one profile on fresh ports and return its record."""
    app_port, admin_port = free_port(), free_port()
    cfg = write_config(profile, app_port, admin_port)
    try:
        srv = server.launch(f"{TOKEN}-{profile}", jar, cfg, TARGET,
                            STATE / "server-logs", app_port, admin_port,
                            ready_timeout=READY_TIMEOUT)
    except server.LaunchError as exc:
        raise Fault(74, f"the tree would not start under the `{profile}` "
                        f"profile; every rung of the launch ladder was tried. "
                        f"{str(exc)[:600]}") from exc
    log(f"{profile}: up on {app_port}/{admin_port} via `{srv.rung}`")
    return {"pid": srv.proc.pid, "app_port": app_port,
            "admin_port": admin_port, "rung": srv.rung, "profile": profile}


def ensure_servers(jar: Path) -> dict:
    """Both profiles running, reusing whatever is still up.

    The negative cache is the same decision as the build's, for the same reason:
    without `.bootfail`, a submission whose jar never binds a port would pay six
    launch ladders on every execution -- an hour each time -- and the round would
    end having asked nothing.
    """
    failed = STATE / ".bootfail"
    if failed.is_file():
        raise Fault(74, f"this tree failed to boot earlier: "
                        f"{failed.read_text().strip()[:400]}")

    path = STATE / "servers.json"
    try:
        records = json.loads(path.read_text())
    except (OSError, ValueError):
        records = {}

    for profile in PROFILES:
        rec = records.get(profile)
        if isinstance(rec, dict) and usable(rec):
            continue
        if isinstance(rec, dict) and isinstance(rec.get("pid"), int):
            # Dead, or alive and not answering.  Either way it may still hold a
            # port, and the replacement takes a fresh one regardless.
            log(f"{profile}: the recorded server is not answering; replacing it")
            kill_group(rec["pid"])
        try:
            records[profile] = boot(profile, jar)
        except Fault as exc:
            failed.write_text(str(exc))
            raise
        path.write_text(json.dumps(records, indent=1))
    return records


def stop_all() -> int:
    """Kill every server this token recorded and forget them.  Used by the image
    build's proof layer, and by hand.  Never called at grade time: the servers
    surviving between candidates is the point."""
    path = STATE / "servers.json"
    try:
        records = json.loads(path.read_text())
    except (OSError, ValueError):
        return 0
    n = 0
    for profile, rec in records.items():
        if isinstance(rec, dict) and isinstance(rec.get("pid"), int):
            kill_group(rec["pid"])
            log(f"{profile}: stopped")
            n += 1
    path.unlink(missing_ok=True)
    return n


# --------------------------------------------------------------------------- #
# 3. The candidate
# --------------------------------------------------------------------------- #
def run_candidate(records: dict) -> int:
    """Run the candidate against these servers and return pytest's exit code."""
    candidate = os.environ["SRB_CANDIDATE"]
    env = dict(os.environ)

    # Everything naming the tree, the token, or a path this module wrote.  A
    # candidate holding any of them can answer "am I on the original?" from the
    # filesystem instead of from the servers: $STATE holds build.log, which names
    # the framework that was compiled.
    for leaked in ("SRB_TARGET", "SRB_TARGET_TOKEN", "SRB_TARGET_NAME",
                   "SRB_TARGET_ROLE", "SRB_ORIGINAL", "SRB_WORK", "SRB_REPO",
                   "SRB_SUITE_WORK", "SRB_ADV_STATE", "OLDPWD"):
        env.pop(leaked, None)

    # Inline, not a file path.  A path would be a foothold in $STATE regardless of
    # what the file itself contains; a JSON document in the environment is the
    # host, the ports and the profile names and nothing else.
    env["SRB_SERVERS"] = json.dumps(
        {p: {"host": "127.0.0.1", "app_port": r["app_port"],
             "admin_port": r["admin_port"]}
         for p, r in records.items()}, sort_keys=True)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"

    # cwd is a fresh empty directory, NOT the tree.  CandidateRunner sets its own
    # cwd to the tree and bash passes that down; inheriting it would put the whole
    # source under `open("pom.xml")`.  PWD is overwritten for the same reason --
    # subprocess sets the working directory but leaves the variable bash exported.
    with tempfile.TemporaryDirectory(prefix="srb-cand-") as neutral:
        env["PWD"] = neutral
        argv = [sys.executable, "-m", "pytest",
                "-p", "no:cacheprovider",   # both roles share the candidates dir
                "-p", "srbcandidate",
                "-q", "--no-header", "-x", f"--timeout={TEST_TIMEOUT}",
                candidate]
        proc = subprocess.run(argv, cwd=neutral, env=env,
                              stdin=subprocess.DEVNULL, check=False)
    log(f"candidate {os.environ.get('SRB_CANDIDATE_NAME', '?')}: "
        f"pytest exit {proc.returncode}")
    return proc.returncode


def main(argv: list[str]) -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    command = argv[1] if len(argv) > 1 else "run"
    if command == "stop":
        stop_all()
        return 0
    if command != "run":
        print(f"usage: {argv[0]} [run|stop]", file=sys.stderr)
        return 70
    try:
        jar = build_tree()
        records = ensure_servers(jar)
    except Fault as exc:
        fault(exc.code, str(exc))
        return exc.code
    except Exception as exc:                       # this module itself broke
        fault(70, f"{type(exc).__name__}: {exc}")
        return 70
    return run_candidate(records)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
