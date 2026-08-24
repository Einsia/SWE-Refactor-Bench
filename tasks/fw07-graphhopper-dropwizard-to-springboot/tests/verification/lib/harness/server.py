"""Launching a GraphHopper server, from either side, without knowing how.

The hard constraint of this stage: State A is launched the way Dropwizard wants
(`java -jar app.jar server config.yml`) and the submission is launched the way
its own framework wants, which the harness cannot know in advance — Spring Boot
alone accepts `java -jar`, `java -jar --spring.config.location=`, and a plain
`java -cp ... MainClass`, and a submission is free to pick any of them.

Hard-coding one invocation would fail correct rewrites for choosing a different
supported form.  Asking the submission to declare one would be a manifest the
agent has to write, and a manifest is a place to lie.

So the launcher DISCOVERS the invocation: it takes the jar the build produced
and tries a small, fixed ladder of standard forms, in order, until one binds the
port and answers HTTP.  The ladder is the same for both sides, so State A is
launched by exactly the mechanism a submission is, and nothing in it is specific
to either framework.  Which rung succeeded is recorded and reported — it is not
graded, because how you start a server is not the behaviour under test, but it
belongs in the log when a launch fails.

What IS graded is what answers once it is up.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import NamedTuple

from swerefactor.contract import submission_env

from . import wire

# The launch ladder.  {jar} and {config} are substituted.
#
# Each rung is a documented, conventional way to start a JVM web application
# with an external config file.  A submission that starts by none of these has
# made its application unlaunchable by any convention, which is a real failure
# of the port and is reported as one.
#
# Order matters: the most specific forms come first so that a jar which accepts
# several is launched by the one that most clearly passes the config, and the
# bare `java -jar {jar}` rung is last because it would otherwise swallow a
# config-taking jar and run it against its built-in defaults.
LADDER = [
    # Dropwizard's own form, and the one State A uses.  Also accepted by any
    # framework that treats argv[0] as a command.
    ("dropwizard-server", ["java", "-jar", "{jar}", "server", "{config}"]),
    # Spring Boot's documented external-config forms.
    ("spring-config-location",
     ["java", "-jar", "{jar}", "--spring.config.location=file:{config}"]),
    ("spring-config-additional",
     ["java", "-jar", "{jar}",
      "--spring.config.additional-location=file:{config}"]),
    # A jar that takes the config as a bare argument.
    ("bare-arg", ["java", "-jar", "{jar}", "{config}"]),
    # A jar that reads a system property.  Both stacks support this shape.
    ("dw-config-property",
     ["java", "-Ddw.server.applicationConnectors[0].port=0", "-jar", "{jar}",
      "server", "{config}"]),
    # And the fallback: no config at all.  Reached only by a jar that embeds its
    # configuration, which will then answer on whatever port it chose — so the
    # port is discovered from the log rather than assumed.
    ("no-config", ["java", "-jar", "{jar}"]),
]


class Server:
    """A running server on a known pair of ports."""

    def __init__(self, name: str, proc: subprocess.Popen, app_port: int,
                 admin_port: int, log: Path, rung: str,
                 admin_ready: bool = False, admin_detail: str = "not probed"):
        self.name = name
        self.proc = proc
        self.app_port = app_port
        self.admin_port = admin_port
        self.log = log
        self.rung = rung
        # Whether the admin port answered at launch, and what it said.  Recorded
        # rather than required: a submission whose admin connector never comes up
        # has broken the handful of cases that ask it, and those cases failing
        # individually is a precise finding.  Raising LaunchError instead would
        # fail the whole module -- the build, the jar, and all 88 cases -- over a
        # port that most of the corpus does not touch.
        self.admin_ready = admin_ready
        self.admin_detail = admin_detail

    def transport(self, port: str) -> wire.Transport:
        return wire.Transport("127.0.0.1",
                              self.app_port if port == "app" else self.admin_port)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def tail(self, lines: int = 40) -> str:
        try:
            return "\n".join(
                self.log.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return "(no log)"

    def cause(self, lines: int = 8, window: int = 400) -> str:
        """The last few lines that say something, stack frames dropped.

        `tail` is the raw window and stays that way -- a caller that wants the log
        as written should get it.  This is for the launch report, where the raw
        window is the wrong thing twice over.

        A JVM that dies on startup writes its message and then thirty frames, so
        the end of the log is frames and the sentence that names the fault is above
        them.  A twelve-line tail of one real failure here was frames only: the
        report said `ConnectionRefusedError` over six lines of io.dropwizard
        plumbing while `java.lang.IllegalStateException: Your specified OSM file
        does not exist:<path>` sat just out of reach.  So the window has to be wide
        enough to contain the message and then filtered down to it, rather than
        merely short.

        `Caused by:` and `... N frames omitted` are kept -- the first is usually the
        real fault and the second is the only sign that frames were dropped at all.
        """
        try:
            raw = self.log.read_text(errors="replace").splitlines()
        except OSError:
            return "(no log)"
        speaking = [ln for ln in raw[-window:]
                    if ln.strip() and not ln.lstrip().startswith("at ")]
        # Nothing but frames in the whole window: a truncated log, or a crash with
        # no message.  The raw tail is then the most honest thing available.
        return "\n".join((speaking or raw)[-lines:])

    def stop(self):
        """SIGTERM the process group, then SIGKILL what is left.

        The group, not the process: `java -jar` under a shell wrapper leaves the
        JVM parented elsewhere, and a submission is free to use one.  A JVM that
        ignores SIGTERM holds its port, and the next launch then fails to bind
        for a reason that has nothing to do with the code under test.
        """
        if self.proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=25)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass


class LaunchError(RuntimeError):
    """A jar that no rung on the ladder could start."""


class Attempt(NamedTuple):
    """One rung's outcome, kept so the failure report can rank the excerpts."""

    rung: str
    detail: str
    cause: str
    log_path: Path
    bound: bool


# How much of the assembled report is allowed to survive.  `Report.record` caps a
# check's detail, and this message is built to fit under that cap rather than to
# be cut by it: six rungs at twelve excerpt lines each is ~10KB, and a tail cut on
# that keeps only the LAST rung -- which on this ladder is `no-config`, the
# fallback that is deliberately given no configuration.  Its failure is therefore
# guaranteed ("If no graph.location is provided you need to specify an OSM file")
# and says nothing about the submission, so every launch failure reported the one
# rung that could not be informative.
#
# The number is below toolchain's 4000 so that the cap is a backstop and not the
# thing shaping the message.  It is duplicated rather than imported because this
# package is the launcher and importing the report writer to learn how long a
# string may be would invert that.
REPORT_BUDGET = 3400


def _excerpt(a: Attempt, budget: int, must_fit: bool) -> str | None:
    """One rung's excerpt within `budget`, or None if it will not fit.

    Trimmed from the FRONT when it has to be.  `cause` is already the tail of the
    log, and within that tail the `Caused by:` chain is last -- so dropping from
    the top loses context and dropping from the bottom loses the fault itself.
    """
    label = (f"  === {a.rung} log excerpt"
             f"{' (this rung had the port open)' if a.bound else ''}:")
    # Every rung writes its own file and the interesting one is rarely the last,
    # so a reader who needs the frames has to be told that frames are what is
    # missing, or the excerpt reads as the whole of what was recorded.
    foot = "      (stack frames elided; full log named above)"
    lines = [f"      {t}" for t in a.cause.splitlines()]
    while lines:
        block = "\n".join([label] + lines + [foot])
        if len(block) <= budget:
            return block
        if not must_fit:
            return None
        lines.pop(0)
        if lines:
            lines[0] = "      [... earlier log lines dropped ...]"
    return None


def _ladder_report(name: str, jar: Path, attempts: list[Attempt]) -> str:
    """The failure text: every rung's verdict, then as many excerpts as fit.

    Two sections, and the split is the point.  The verdict lines come first and
    are never dropped, so a reader always learns what all six rungs did -- that
    set is small, and it is what distinguishes "this jar takes none of these
    forms" from "this jar starts and then dies", which are different bugs with
    different fixes.

    Excerpts come after, most-informative first: a rung that got the app port
    answering reached application startup, so its log holds the fault, while a
    rung that never bound most likely rejected its arguments.  Ladder order
    breaks ties, so the most specific form is shown before the fallbacks.

    On the real fw07 submission this is the difference between reporting

        If no graph.location is provided you need to specify an OSM file

    -- the fallback rung, which was never given a config -- and reporting

        Failed to start bean 'childManagementContextInitializer'
        IllegalStateException: failed to start admin server
        IOException: Failed to bind to /0.0.0.0:19990
        BindException: Address already in use

    which is the actual reason and names the port that was double-bound.
    """
    head = [f"no launch form started {name} from {jar.name}:"]
    for a in attempts:
        head.append(f"  --- {a.rung}: {a.detail}")
        # The log path on the verdict line, not only in the excerpt block: a rung
        # whose excerpt did not fit would otherwise be named with no way to reach
        # what it wrote, which is the one thing a reader needs next.
        head.append(f"        full log: {a.log_path}")
    if any(a.bound for a in attempts):
        # Once, rather than on each bound rung's verdict line.  Three rungs times
        # this sentence was 400 characters of the budget and left no room for a
        # single excerpt, so the report explained the failure mode and showed none
        # of the evidence for it.
        head.append("  (a rung above that answered and then stopped had opened "
                    "its port during startup and lost it again: a context that "
                    "binds its connector before it finishes wiring answers "
                    "inside the window in which it is already failing.)")
    # A rung whose log came out empty has nothing to excerpt, and calling it
    # "dropped for length" would be false -- a reader chasing a named log that
    # turns out to be empty spends the trip finding out the report misled them.
    # A process killed before it wrote anything is the normal way this happens, so
    # it is stated on its own line and does not compete for excerpt space.
    silent = [a for a in attempts if a.cause.strip() in ("", "(no log)")]
    if silent:
        head.append("  (wrote nothing to its log: "
                    + ", ".join(a.rung for a in silent) + ")")
    text = "\n".join(head)

    # By identity, not equality: Attempt is a NamedTuple, so two rungs that failed
    # the same way with the same empty log compare equal and `not in` would drop
    # both on the first match.
    speaking = [a for a in attempts if not any(a is s for s in silent)]
    # Room for the omitted-rungs notice, reserved BEFORE placing excerpts: it is
    # appended last, and budgeting the excerpts without it let the finished report
    # run past the cap it exists to stay under.  Worst case is every rung named.
    reserve = len(_omission_notice(speaking)) + 1 if speaking else 0

    # Most-informative first, ladder order within each group.  `enumerate` keeps
    # the sort stable on Python versions where that is not already guaranteed for
    # the key alone.
    ranked = sorted(enumerate(speaking), key=lambda p: (not p[1].bound, p[0]))
    shown, omitted = [], []
    for i, (_, a) in enumerate(ranked):
        # The first one is guaranteed, trimmed if it has to be.  A report with no
        # excerpt at all describes the failure without showing it, which is the
        # state this function was written to get out of -- and the top-ranked rung
        # is the one whose log holds the fault.
        budget = REPORT_BUDGET - len(text) - 1 - reserve
        candidate = _excerpt(a, budget, must_fit=(i == 0))
        if candidate is None:
            omitted.append(a)
            continue
        shown.append(candidate)
        text = text + "\n" + candidate

    parts = head + shown
    if omitted:
        # Named, not silently dropped: a reader who is shown one excerpt and not
        # told there were six will read the one as the whole ladder.
        parts.append(_omission_notice(omitted))
    return "\n".join(parts)


def _omission_notice(omitted: list[Attempt]) -> str:
    return (f"  ({len(omitted)} further rung(s) not excerpted here for length: "
            + ", ".join(a.rung for a in omitted)
            + ".  Each one's verdict and log path are listed above.)")


def _settled(proc: subprocess.Popen, app: wire.Transport, path: str,
             settle: float = 2.0) -> tuple[bool, str]:
    """True when the app port is still answering `settle` seconds later.

    `path` is the caller's readiness path, and asking the same one twice is the
    point: a different path could hang where the first answered -- a catch-all
    handler that blocks would burn the transport's full 120s timeout and fail a
    server that had already proved it was up.  The question here is only whether
    what answered still answers.

    Spring Boot opens the servlet container's connector during `onRefresh`,
    before the rest of the context is wired.  A bean that throws after that
    point tears the context down and closes the port again, so there is a window
    in which the socket accepts connections and the process is already dying.
    Measured at 172ms on one real fw07 submission, whose own admin server and
    Spring's management context both tried to bind the admin port:

        .577  Started ServerConnector{0.0.0.0:19989}   <- app port opens
        .733  Jetty started on port 19990
        .738  WARN failed to start admin server  BindException
        .749  Stopped ServerConnector{0.0.0.0:19989}   <- app port closes

    A single probe inside that window reports a started server.  The ladder then
    stops trying rungs, records the launch as a pass at full weight, and every
    one of the 88 cases fails with ConnectionRefusedError against a JVM that
    exited seconds earlier -- so the report says the submission started *and*
    answered nothing, which a reader cannot resolve into a cause.

    Worse, which rung "wins" becomes a race: on that same submission the first
    rung held the port for ~130ms and was missed, the fifth held it for 172ms
    and was caught.  Two runs of one unchanged tree could disagree about whether
    it launched at all.

    So readiness is asked twice with a pause between: once to find the port, and
    once to establish that it stayed.
    """
    time.sleep(settle)
    # Terse on purpose.  Six of these are listed in one failure report, and the
    # explanation of WHY a transient bind happens is the same sentence every time
    # -- repeating it per rung crowded the excerpts out of the report entirely.
    # `_ladder_report` states it once, and these carry only the facts that differ.
    if proc.poll() is not None:
        return False, (f"the app port answered, then the process exited with "
                       f"status {proc.returncode} within {settle:g}s")
    ok, detail = app.wait_ready(path, time.monotonic() + 10.0)
    if ok:
        return True, ""
    return False, (f"the app port answered, then stopped within {settle:g}s "
                   f"while the process stayed alive: {detail}")


def _spawn(cmd: list[str], cwd: Path, log_path: Path,
           env: dict) -> subprocess.Popen:
    log = open(log_path, "wb")
    return subprocess.Popen(
        cmd, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, env=env,
        # Its own process group, so stop() can signal the whole tree and one
        # side's shutdown cannot reach the other's JVM.
        start_new_session=True,
    )


def launch(name: str, jar: Path, config: Path, workdir: Path, log_dir: Path,
           app_port: int, admin_port: int, ready_timeout: float = 420.0,
           env_extra: dict | None = None) -> Server:
    """Start `jar` and return once it answers HTTP, or raise LaunchError.

    ready_timeout is generous because a cold start imports the OSM extract and
    runs the CH and LM preparations — minutes on the PT profile, and the same
    cost on both sides.  A timeout that fitted a warm start would fail both.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    attempts = []
    for rung, template in LADDER:
        cmd = [part.format(jar=str(jar), config=str(config))
               for part in template]
        log_path = log_dir / f"{name}-{rung}.log"
        # `submission_env` scrubs the eight SRB_* contract names, which name the
        # tests directory and the result file: this is the submission's own
        # server, and it should not be handed them.  SRB_APP_PORT and
        # SRB_ADMIN_PORT below are not contract names and pass through -- they
        # are addressed to the submission on purpose.
        env = submission_env()
        # The ports the config file names, also exported so a submission that
        # reads them from the environment (a legitimate Spring Boot idiom) binds
        # where this harness is looking.  Both sides get the identical set.
        env.update({
            "SRB_APP_PORT": str(app_port),
            "SRB_ADMIN_PORT": str(admin_port),
            "SERVER_PORT": str(app_port),
            "MANAGEMENT_SERVER_PORT": str(admin_port),
            "JAVA_TOOL_OPTIONS": "-Xmx1500m -Duser.timezone=UTC "
                                 "-Dfile.encoding=UTF-8",
        })
        if env_extra:
            env.update(env_extra)

        proc = _spawn(cmd, workdir, log_path, env)
        app = wire.Transport("127.0.0.1", app_port)
        deadline = time.monotonic() + ready_timeout
        ready = False
        # Whether this rung ever got the app port to answer, even once.  It ranks
        # the excerpts in the failure report: a rung that opened the port and then
        # lost it got deep into application startup, and its log holds the fault.
        # A rung that never bound usually died on its arguments, which says
        # nothing about the port beyond "this form is not the one".
        bound = False
        detail = "process exited before binding"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                # It exited, and that is the finding -- so say so, rather than
                # leaving the last probe's error in `detail` to be reported.  A
                # dead process refuses connections by definition, and a report
                # blaming ConnectionRefusedError describes the probe instead of the
                # JVM: it reads as a timing or port problem when what happened is
                # that the server ran and quit.  Which of the two it was decides
                # where a submitter looks next, so the exit status is named.
                detail = (f"the process exited with status {proc.returncode} "
                          f"before binding a port")
                break
            ok, probe_detail = app.wait_ready("/info", min(
                time.monotonic() + 10.0, deadline))
            if ok:
                # It answered.  Whether it MEANT it is a second question -- see
                # `_settled`.  A bind that does not outlive the probe is not a
                # started server, and accepting one turns the rest of the module
                # into 88 connection refusals against a JVM that already exited.
                bound = True
                ready, settle_detail = _settled(proc, app, "/info")
                if ready:
                    break
                # Not a slow start: the port was there and went away.  Polling on
                # would either time out or catch a second transient, so stop and
                # let the ladder move to the next rung with this as the finding.
                detail = settle_detail
                break
            detail = probe_detail
        else:
            detail = (f"still not answering after {ready_timeout:g}s; the process "
                      f"is alive, so it is starting too slowly or listening "
                      f"elsewhere")
        if ready:
            # The admin port, on a short deadline: it comes up alongside the app
            # port or not at all, so a long wait here would only delay the report.
            # `/` and not `/healthcheck` or `/actuator/health`, because which of
            # those exists is a framework choice and readiness only asks whether
            # something is speaking HTTP there.
            admin_ok, admin_detail = wire.Transport(
                "127.0.0.1", admin_port).wait_ready(
                    "/", time.monotonic() + 20.0)
            return Server(name, proc, app_port, admin_port, log_path, rung,
                          admin_ready=admin_ok, admin_detail=admin_detail)

        # Not this rung.  Kill it before trying the next, or two JVMs contend
        # for the port and the next rung fails for the wrong reason.
        srv = Server(name, proc, app_port, admin_port, log_path, rung)
        srv.stop()
        # `bound` is what ranks the excerpts below: a rung that got the port open
        # got further into the application than one that died parsing its
        # arguments, so its log is where the fault is.
        attempts.append(Attempt(rung, detail, srv.cause(12), log_path, bound))

    raise LaunchError(_ladder_report(name, jar, attempts))
