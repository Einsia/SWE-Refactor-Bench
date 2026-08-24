"""The launch ladder, and what it is allowed to call a started server.

Not part of the graded suite: every module runs a declared command against its
own directory (`run-pytest.sh` collects `$SRB_MODULE_DIR` only), so nothing here
is collected at grading time.  It is here because `launch` decides a weight-2.0
check on both sides of the comparison, and it decided one wrongly.

The bug these were written for: `launch` probed the app port once, and Spring Boot
opens that connector during `onRefresh` -- before the context has finished wiring.
A bean that throws afterwards closes it again.  On a real fw07 submission the
window was 172ms, the probe landed inside it, and the harness recorded

    start-submission-base  PASS  "... and answered on both ports"

against a JVM that had already exited.  All 88 cases then came back
ConnectionRefusedError, so the report said the submission started and answered
nothing.  The admin port in that sentence had never been probed at all.

It was also a race: the first rung's window was ~130ms and was missed, the fifth's
was 172ms and was caught, so which rung "won" -- and whether the launch check
passed at all -- depended on timing rather than on the tree.

Run:  PYTHONPATH=<infra>:<behavioural>/lib python3 -m pytest lib/harness/test_server.py
"""
from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import toolchain
from swerefactor.contract import submission_env

from harness import server, wire

# A fake server, in place of a JVM.  It speaks just enough HTTP for
# `wire.Transport` to parse a reply, and takes its ports from the environment the
# launcher exports -- which is the same contract a submission has.
FAKE = textwrap.dedent('''
    import os, socket, sys, threading, time

    mode = sys.argv[1]
    app = int(os.environ["SRB_APP_PORT"])
    admin = int(os.environ["SRB_ADMIN_PORT"])
    HELLO = b"HTTP/1.1 200 OK\\r\\nContent-Length: 2\\r\\nConnection: close\\r\\n\\r\\nok"

    def say(*lines):
        """Log like a JVM: a message, then frames.

        Not decoration.  `_spawn` sends stdout to the rung's log, `cause` filters
        `at ` frames out of a wide window to find the message above them, and
        `_ladder_report` excerpts what survives.  A fake that printed nothing left
        every rung with an empty log, so the end-to-end test measured the ranking
        and assembly of nothing at all and passed on an IndexError-shaped hole.
        `flush` because this stream is a file, and a mode that is killed rather
        than allowed to exit would otherwise take its buffer with it.
        """
        for line in lines:
            print(line, flush=True)

    say(f"INFO  [main] fake_server: mode={mode} app={app} admin={admin}",
        "INFO  [main] fake_server: starting")

    def serve(sock, stop=None):
        sock.settimeout(0.2)
        while stop is None or time.monotonic() < stop:
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                conn.recv(65536)
                conn.sendall(HELLO)
            except OSError:
                pass
            finally:
                conn.close()

    def listener(port):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(16)
        return s

    if mode == "stay":
        a = listener(app)
        threading.Thread(target=serve, args=(listener(admin),),
                         daemon=True).start()
        serve(a)

    elif mode == "no-admin":
        serve(listener(app))

    elif mode == "flash-and-die":
        a = listener(app)
        serve(a, stop=time.monotonic() + 1.0)
        a.close()
        # run1's shape: the connector opened, a later bean threw, the context tore
        # down.  Frames after the message, because that ordering is what `cause`
        # exists to see past.
        say("ERROR [main] fake_server: context failed to refresh",
            "java.lang.IllegalStateException: failed to start admin server",
            "\\tat fake.Admin.start(Admin.java:41)",
            "\\tat fake.App.run(App.java:88)",
            "Caused by: java.net.BindException: Address already in use",
            "\\tat java.base/sun.nio.ch.Net.bind(Net.java:555)",
            "\\t... 24 frames omitted")
        sys.exit(3)

    elif mode == "flash-and-linger":
        a = listener(app)
        serve(a, stop=time.monotonic() + 1.0)
        a.close()
        say("ERROR [main] fake_server: connector closed, non-daemon thread alive",
            "java.lang.IllegalStateException: connector stopped after startup",
            "\\tat fake.Connector.stop(Connector.java:12)")
        time.sleep(60)

    elif mode == "never-binds":
        # A rung whose form was rejected before anything bound: no port, and a
        # message about the arguments rather than about a socket.
        say("ERROR [main] fake_server: unrecognised argument form",
            "java.lang.IllegalArgumentException: no such command: server",
            "\\tat fake.Cli.parse(Cli.java:7)")
        sys.exit(9)
''')


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def fake(tmp_path: Path) -> Path:
    path = tmp_path / "fake_server.py"
    path.write_text(FAKE, encoding="utf-8")
    return path


def spawn(fake: Path, mode: str, app_port: int,
          admin_port: int) -> subprocess.Popen:
    """The fake, started the way `launch` starts a submission.

    `submission_env` rather than a copy of the ambient environment: the contract
    names are scrubbed for a submission's own server, and a helper here that
    copied them would both diverge from the code under test and trip the
    suite-wide isolation check that exists to catch exactly that.
    """
    env = submission_env()
    env.update({"SRB_APP_PORT": str(app_port), "SRB_ADMIN_PORT": str(admin_port)})
    return subprocess.Popen([sys.executable, str(fake), mode], env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True)


def wait_bound(port: int, timeout: float = 10.0) -> bool:
    """Block until something answers HTTP on `port`, as the launcher would."""
    ok, _ = wire.Transport("127.0.0.1", port).wait_ready(
        "/", time.monotonic() + timeout)
    return ok


# --------------------------------------------------------------------------- #
# the settle check
# --------------------------------------------------------------------------- #


def test_a_port_that_stays_open_settles(fake):
    app_port, admin_port = free_port(), free_port()
    proc = spawn(fake, "stay", app_port, admin_port)
    try:
        assert wait_bound(app_port)
        ok, detail = server._settled(
            proc, wire.Transport("127.0.0.1", app_port), "/info", settle=0.5)
        assert ok, detail
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_bind_that_does_not_outlive_the_probe_is_not_a_launch(fake):
    """The 172ms window, reproduced: answering once is not being up."""
    app_port, admin_port = free_port(), free_port()
    proc = spawn(fake, "flash-and-die", app_port, admin_port)
    try:
        assert wait_bound(app_port), "the fake never bound; the test proves nothing"
        ok, detail = server._settled(
            proc, wire.Transport("127.0.0.1", app_port), "/info", settle=2.0)
        assert not ok
        # And the detail has to name the exit, not the refused connection: a
        # reader who is told ConnectionRefusedError looks at ports and timing,
        # when what happened is that the process ran and quit.
        assert "exited with status 3" in detail
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_a_port_that_closes_while_the_process_lives_is_not_a_launch(fake):
    """The other half: still running, no longer listening.

    A context that closes its connector and leaves a non-daemon thread alive
    looks healthy to `poll()` and answers nothing.  Checking only liveness would
    accept it.
    """
    app_port, admin_port = free_port(), free_port()
    proc = spawn(fake, "flash-and-linger", app_port, admin_port)
    try:
        assert wait_bound(app_port), "the fake never bound; the test proves nothing"
        ok, detail = server._settled(
            proc, wire.Transport("127.0.0.1", app_port), "/info", settle=2.0)
        assert not ok
        assert "stopped" in detail and "process stayed alive" in detail
    finally:
        proc.kill()
        proc.wait(timeout=10)


# --------------------------------------------------------------------------- #
# the ladder
# --------------------------------------------------------------------------- #
# `launch` hard-codes `java -jar`, so the ladder is replaced with rungs that run
# the fake under the same substitution: {jar} is the script.  Everything else --
# the ordering, the settle check, the per-rung kill, the admin probe, the
# LaunchError text -- is the real code path.


def ladder(monkeypatch, *modes: str) -> None:
    monkeypatch.setattr(server, "LADDER", [
        (mode, [sys.executable, "{jar}", mode]) for mode in modes])


def run_launch(fake: Path, tmp_path: Path, app_port: int, admin_port: int,
               timeout: float = 30.0) -> server.Server:
    return server.launch("side", fake, tmp_path / "cfg.yml", tmp_path,
                         tmp_path / "logs", app_port, admin_port,
                         ready_timeout=timeout)


def test_the_ladder_passes_over_a_rung_that_only_flashes(fake, tmp_path,
                                                         monkeypatch):
    """The whole bug, at the level that decides the score.

    Before the settle check this returned on the first rung with a dead process,
    and `ask_side` then recorded 88 connection refusals against it.
    """
    ladder(monkeypatch, "flash-and-die", "stay")
    app_port, admin_port = free_port(), free_port()
    srv = run_launch(fake, tmp_path, app_port, admin_port)
    try:
        assert srv.rung == "stay"
        assert srv.alive()
    finally:
        srv.stop()


def test_a_server_that_only_flashes_on_every_rung_does_not_launch(fake, tmp_path,
                                                                  monkeypatch):
    ladder(monkeypatch, "flash-and-die", "flash-and-die")
    app_port, admin_port = free_port(), free_port()
    with pytest.raises(server.LaunchError) as exc:
        run_launch(fake, tmp_path, app_port, admin_port)
    text = str(exc.value)
    # Both rungs reported, and the reason names the transient bind rather than
    # leaving a reader with "connection refused" on a port that did open.
    assert text.count("--- flash-and-die:") == 2
    assert "exited with status 3" in text


def test_a_launched_server_records_whether_the_admin_port_answered(fake, tmp_path,
                                                                   monkeypatch):
    ladder(monkeypatch, "stay")
    app_port, admin_port = free_port(), free_port()
    srv = run_launch(fake, tmp_path, app_port, admin_port)
    try:
        assert srv.admin_ready is True
        assert "HTTP 200" in srv.admin_detail
    finally:
        srv.stop()


def test_a_missing_admin_port_is_recorded_not_fatal(fake, tmp_path, monkeypatch):
    """A missing admin port is recorded against the admin cases, not raised.

    Not fatal on purpose: raising here would fail the build, the jar and all 88
    cases over a port most of the corpus does not touch.  The admin cases failing
    on their own is the precise finding; this is the line that explains them.
    """
    ladder(monkeypatch, "no-admin")
    app_port, admin_port = free_port(), free_port()
    srv = run_launch(fake, tmp_path, app_port, admin_port)
    try:
        assert srv.alive()
        assert srv.admin_ready is False
        assert srv.admin_detail and "HTTP" not in srv.admin_detail
    finally:
        srv.stop()


def test_a_process_that_never_binds_is_reported_as_having_exited(fake, tmp_path,
                                                                 monkeypatch):
    ladder(monkeypatch, "never-binds")
    app_port, admin_port = free_port(), free_port()
    with pytest.raises(server.LaunchError) as exc:
        run_launch(fake, tmp_path, app_port, admin_port)
    assert "exited with status 9" in str(exc.value)


def test_the_first_rung_that_stays_up_wins(fake, tmp_path, monkeypatch):
    """Ordering is load-bearing: the ladder is most-specific-first."""
    ladder(monkeypatch, "stay", "no-admin")
    app_port, admin_port = free_port(), free_port()
    srv = run_launch(fake, tmp_path, app_port, admin_port)
    try:
        assert srv.rung == "stay"
    finally:
        srv.stop()


# --------------------------------------------------------------------------- #
# the failure report
# --------------------------------------------------------------------------- #
# Six rungs at twelve excerpt lines each is ~10KB, and `toolchain.Report.record`
# keeps the last 4000 characters.  A fixed excerpt per rung against a tail cap
# therefore reports the *last* rung and drops the rest -- and on this ladder the
# last rung is `no-config`, the fallback that is deliberately given no
# configuration.  Its failure is guaranteed and says nothing about the submission:
#
#     If no graph.location is provided you need to specify an OSM file
#
# while the rungs that got the port open, and the BindException that would be the
# actual reason, are the part that gets cut.  The checks below hold the report
# under the cap by budgeting the excerpt against the number of rungs, so the
# informative rung survives the truncation.


def attempt(rung: str, bound: bool, cause_lines: int = 12,
            detail: str = "") -> server.Attempt:
    body = "\n".join(f"{rung} cause line {i} " + "x" * 120
                     for i in range(cause_lines))
    return server.Attempt(rung, detail or f"{rung} did not start", body,
                          Path(f"/work/server-logs/{rung}.log"), bound)


LADDER_RUNGS = ["dropwizard-server", "spring-config-location",
                "spring-config-additional", "bare-arg", "dw-config-property",
                "no-config"]


def test_every_rungs_verdict_survives_the_budget():
    """The set of verdicts is what distinguishes two different bugs.

    "no form starts this jar" and "it starts and then dies" need different fixes,
    and only the full set of six tells them apart.  Excerpts are droppable; these
    lines are not.
    """
    report = server._ladder_report(
        "submission-base", Path("app.jar"),
        [attempt(r, bound=(r == "dropwizard-server")) for r in LADDER_RUNGS])
    for rung in LADDER_RUNGS:
        assert f"--- {rung}: {rung} did not start" in report


def test_the_report_fits_under_the_caps_that_used_to_cut_it():
    """The invariant the whole fix rests on.

    `Report.record` clips, and clipping must be a backstop rather than the thing
    deciding what a reader sees.  Six rungs of full-length excerpts is the worst
    case, so if that fits, nothing is cut by the cap.
    """
    report = server._ladder_report(
        "submission-base", Path("graphhopper-web-11.0-SNAPSHOT.jar"),
        [attempt(r, bound=(r in ("dropwizard-server", "bare-arg",
                                 "dw-config-property")))
         for r in LADDER_RUNGS])
    assert len(report) <= server.REPORT_BUDGET
    assert len(report) <= toolchain.DETAIL_CAP
    # And clipping it really is a no-op, rather than merely close to one.
    assert toolchain.clip(report) == report


def test_a_rung_that_had_the_port_open_is_excerpted_before_one_that_did_not():
    """The rung that reached application startup is the one holding the fault.

    run1's real ordering: `no-config` never bound and failed on a missing OSM
    file, which is what the fallback always does.  `dropwizard-server` got the
    port open and died on the double-bound admin port.  The second is the finding.
    """
    report = server._ladder_report(
        "submission-base", Path("app.jar"),
        [attempt("no-config", bound=False),
         attempt("dropwizard-server", bound=True)])
    excerpts = [ln for ln in report.splitlines() if ln.startswith("  === ")]
    assert "dropwizard-server" in excerpts[0]
    assert "(this rung had the port open)" in excerpts[0]


def test_rungs_dropped_for_length_are_named_not_silently_lost():
    """One excerpt reads as the whole ladder unless the report says otherwise.

    Six full-length excerpts do not fit, which is the normal case rather than an
    edge one -- so what a reader is NOT being shown has to be stated, and every
    dropped rung still has to carry its verdict and a path to its log.
    """
    attempts = [attempt(r, bound=False) for r in LADDER_RUNGS]
    report = server._ladder_report("submission-base", Path("app.jar"), attempts)
    assert len(report) <= server.REPORT_BUDGET
    shown = {r for r in LADDER_RUNGS if f"  === {r} log excerpt" in report}
    missing = [r for r in LADDER_RUNGS if r not in shown]
    assert missing, "six full excerpts should not all fit; else this proves nothing"
    assert "not excerpted here for length" in report
    notice = report.split("not excerpted here for length")[1]
    for rung in missing:
        assert rung in notice
        # And still reachable: the verdict line carries the log path, so a rung
        # without an excerpt is not a dead end.
        assert f"full log: /work/server-logs/{rung}.log" in report


def test_the_top_ranked_excerpt_is_kept_even_when_it_must_be_trimmed():
    """A report with no excerpt at all describes a failure without showing it.

    That is the state this assembly exists to get out of, so the highest-ranked
    rung's excerpt is guaranteed and trimmed to fit rather than dropped.  Trimmed
    from the FRONT: `cause` is already a tail, and the `Caused by:` chain is at the
    end of it, so cutting from the bottom would remove the fault itself.
    """
    attempts = [attempt(r, bound=False, cause_lines=200) for r in LADDER_RUNGS]
    attempts[0] = attempts[0]._replace(
        cause="\n".join(["noise " + "y" * 200] * 200 + ["Caused by: THE FAULT"]))
    report = server._ladder_report("submission-base", Path("app.jar"), attempts)
    assert len(report) <= server.REPORT_BUDGET
    assert f"  === {LADDER_RUNGS[0]} log excerpt" in report
    assert "Caused by: THE FAULT" in report
    assert "earlier log lines dropped" in report
    for rung in LADDER_RUNGS:
        assert f"--- {rung}:" in report
    assert "not excerpted here for length" in report


def test_the_transient_bind_explanation_is_stated_once():
    """Per-rung it was 130 characters times three, and crowded out every excerpt.

    The facts that differ stay on the verdict lines; the sentence explaining the
    failure mode belongs once, above them.
    """
    report = server._ladder_report(
        "submission-base", Path("app.jar"),
        [attempt(r, bound=(r != "no-config")) for r in LADDER_RUNGS])
    assert report.count("inside the window in which it is already failing") == 1
    # And it is absent entirely when no rung ever got a port open, because then it
    # would be explaining something that did not happen.
    none_bound = server._ladder_report(
        "submission-base", Path("app.jar"),
        [attempt(r, bound=False) for r in LADDER_RUNGS])
    assert "inside the window" not in none_bound


def test_a_rung_with_an_empty_log_says_so_instead_of_claiming_it_was_dropped():
    """An empty log is a different fact from a log that would not fit.

    `cause` returns "" for a process killed before it wrote anything, and
    "(no log)" when the file cannot be read at all.  Reporting either as "not
    excerpted here for length" sends a reader to a path that has nothing in it and
    tells them the report chose not to show it -- so they look for the missing
    output rather than for the reason there is none.
    """
    attempts = [attempt("dropwizard-server", bound=True)._replace(cause=""),
                attempt("no-config", bound=False)._replace(cause="(no log)"),
                attempt("bare-arg", bound=True)]
    report = server._ladder_report("submission-base", Path("app.jar"), attempts)
    assert "wrote nothing to its log: dropwizard-server, no-config" in report
    # Neither is claimed as an omission, and the one with content is still shown.
    if "not excerpted here for length" in report:
        notice = report.split("not excerpted here for length")[1]
        assert "dropwizard-server" not in notice and "no-config" not in notice
    assert "  === bare-arg log excerpt" in report
    # Every rung keeps its verdict line and its log path regardless.
    for rung in ("dropwizard-server", "no-config", "bare-arg"):
        assert f"--- {rung}:" in report
        assert f"full log: /work/server-logs/{rung}.log" in report


def test_a_ladder_where_no_rung_logged_anything_still_reports_the_verdicts():
    """The degenerate case: nothing to excerpt, and the report must not be empty.

    This is what the end-to-end test was hitting without noticing -- the fake
    printed nothing, so every rung was silent and no excerpt rendered.  The
    verdicts are the whole report here, and they have to be enough to act on.
    """
    attempts = [attempt(r, bound=False)._replace(cause="") for r in LADDER_RUNGS]
    report = server._ladder_report("submission-base", Path("app.jar"), attempts)
    assert "  === " not in report
    assert "not excerpted here for length" not in report
    for rung in LADDER_RUNGS:
        assert f"--- {rung}: {rung} did not start" in report
    assert len(report) <= server.REPORT_BUDGET


def test_a_real_ladder_failure_names_the_reason_not_the_fallbacks(fake, tmp_path,
                                                                  monkeypatch):
    """End to end, through the code that assembles the message.

    Two flashing rungs and a bare exit, which is run1's shape: the rungs that got
    the port open must be the ones excerpted, and the reader must be told all
    three verdicts.
    """
    ladder(monkeypatch, "flash-and-die", "never-binds", "flash-and-linger")
    app_port, admin_port = free_port(), free_port()
    with pytest.raises(server.LaunchError) as exc:
        run_launch(fake, tmp_path, app_port, admin_port)
    text = str(exc.value)
    assert len(text) <= toolchain.DETAIL_CAP
    for rung in ("flash-and-die", "never-binds", "flash-and-linger"):
        assert f"--- {rung}:" in text
    # The rung that never bound reports its exit; the ones that did report the
    # transient bind.  Both statements have to be present and attributable.
    assert "exited with status 9" in text
    assert "exited with status 3" in text
    excerpts = [ln for ln in text.splitlines() if ln.startswith("  === ")]
    assert excerpts, "no excerpt rendered at all; the report shows no evidence"
    assert "never-binds" not in excerpts[0]
    # And the excerpt carries the fault rather than the frames above it: this is
    # the `Caused by: BindException` line's stand-in, and it is the entire reason
    # the report is assembled instead of tail-cut.
    assert "Caused by: java.net.BindException" in text
    assert "at fake.Admin.start" not in text          # frames dropped by `cause`


# --------------------------------------------------------------------------- #
# clipping a detail
# --------------------------------------------------------------------------- #


def test_clip_keeps_both_ends_and_says_what_it_dropped():
    """A tail cut hid the front of every long diagnostic.

    The launcher now fits under the cap, so this is about the next caller that
    does not: losing the head silently is what made the ladder report unreadable,
    and a message that starts mid-word gives a reader no way to know why.
    """
    text = "HEAD-MARKER\n" + ("filler line\n" * 2000) + "TAIL-MARKER"
    out = toolchain.clip(text)
    assert len(out) <= toolchain.DETAIL_CAP + 200      # plus the elision notice
    assert "HEAD-MARKER" in out
    assert "TAIL-MARKER" in out
    assert "elided from the middle" in out


def test_clip_leaves_a_detail_that_fits_exactly_as_it_was():
    text = "short enough to survive\nwith two lines"
    assert toolchain.clip(text) == text
