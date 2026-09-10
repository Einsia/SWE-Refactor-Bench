#!/usr/bin/env python3
"""Compile the submission and play the whole corpus against it, once.

This is the only module that touches the submitted tree, and the only one that
starts a server.  Everything else in this suite reads the recording it publishes
at ``$SRB_SUITE_WORK/actual.json``.  Three reasons that split is worth a module of
its own:

*   **The submission cannot tell which test is looking at it.**  Every request it
    will ever answer arrives during this one replay, in one order, before any
    comparison happens.  There is no observable difference between the run that
    grades archives and the run that grades auth, so behaviour cannot be varied
    per test.
*   **It is measured once.**  Thirteen modules booting 62 servers each would be
    806 boots, and would let a flaky port score differently in each module.
*   **A build takes minutes.**  ``lto = true`` with ``codegen-units = 1`` is in the
    baseline's release profile and is not something to pay thirteen times.  This
    is exactly what the runner documents ``SRB_SUITE_WORK`` for.

What is graded here is narrow on purpose: that it builds, that the binary runs,
that every configuration boots, and that the replay completed.  *How* it answered
is the other modules' business.  A boot failure is recorded per session rather
than raised, so one missing flag costs that session's cases instead of the run --
but a build failure is fatal, because a recording of nothing would let every
aggregate check pass by finding nothing to object to.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

# lib/ is on PYTHONPATH already -- the runner puts it there for every module.
import replay
from harness import corpus
from harness.runner import TREE_SPEC, ServerFailed, play_session

REPO = Path(os.environ.get("SRB_REPO", "/workspace/repo"))
WORK = Path(os.environ.get("SRB_WORK", "/tmp/srb-build"))
SHARED = Path(os.environ["SRB_SUITE_WORK"])
RESULT = Path(os.environ["SRB_RESULT"])

CHECKS: list[dict] = []

# Three deadlines, and their ORDER is the property.  `suite.toml` gives this
# module 5400s; the compile gets 3600s of that, and `finishes_in_budget` reports
# on anything past 3000s.  The compile's deadline has to stay below the module's,
# so that a build which runs long is *recorded here* rather than killed by the
# harness -- which can only say the module as a whole ran out of time, and says it
# with `checks: []`.  Raising BUILD_TIMEOUT_SEC above 5400 silently swaps which
# deadline fires and throws away every check this module had already recorded.
BUILD_TIMEOUT_SEC = 3600
BUILD_BUDGET_SEC = 3000
VERSION_TIMEOUT_SEC = 60


def _text(stream: object) -> str:
    """A TimeoutExpired's captured output, which may be str, bytes or None."""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", "replace")
    return stream if isinstance(stream, str) else ""


def record(check_id: str, ok: bool, weight: float, summary: str,
           detail: str = "", required: bool = False) -> bool:
    entry = {"id": check_id, "verdict": "pass" if ok else "fail",
             "weight": weight, "summary": summary if not ok else ""}
    if detail and not ok:
        entry["detail"] = detail[-4000:]
    if required:
        entry["required"] = True
    CHECKS.append(entry)
    print(f"[{'ok  ' if ok else 'FAIL'}] {check_id}: {summary}", flush=True)
    return ok


def flush() -> None:
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps({"checks": CHECKS}, indent=1), encoding="utf-8")


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    SHARED.mkdir(parents=True, exist_ok=True)

    if not (REPO / "Cargo.toml").is_file():
        record("build.present", False, 1.0,
               f"no Cargo.toml under {REPO}: there is no crate to build",
               required=True)
        flush()
        return 1

    # ----------------------------------------------------------------- build
    # Built into a target dir outside the tree, so nothing this module does is
    # visible to stage 3, which compares against the tree as submitted.
    #
    # $SRB_WARM_TARGET, when the image sets it, is a target directory already
    # holding the dependency closure compiled with the baseline's release profile.
    # Using it is a fairness question rather than an optimisation: the agent
    # developed against a warm cache the environment image built for it, so a cold
    # grader would be timing a different build from the one the agent measured --
    # and `lto = true` with `codegen-units = 1` over ~340 crates is where nearly
    # all of that time goes.  What still compiles here is the submission's own
    # code, which is the part being graded.  A submission that changed its
    # dependency versions gets less out of the cache, exactly as it would in the
    # agent container, because cargo rebuilds what does not match.
    warm = os.environ.get("SRB_WARM_TARGET", "")
    target = Path(warm) if warm and os.access(warm, os.W_OK) else WORK / "target"

    # Whatever the cache holds, it must not be able to supply the artefact this
    # module is about to grade.  `build.produces_binary` is a required check, and a
    # stale `release/miniserve` sitting in a shared directory would turn it into a
    # free pass for a submission that compiled nothing.  The warm cache is built
    # from a synthetic crate that produces no such binary, and the image asserts
    # that; removing it here as well means the property holds even if someone later
    # points SRB_WARM_TARGET at a directory where it does not.
    for stale in (target / "release" / "miniserve", target / "debug" / "miniserve"):
        if stale.exists() or stale.is_symlink():
            stale.unlink()
    env = dict(os.environ, CARGO_TERM_COLOR="never", RUSTFLAGS="")
    started = time.monotonic()
    # Caught rather than allowed to propagate.  An uncaught TimeoutExpired leaves
    # `main()` before `flush()`, so the module writes no result file at all; the
    # harness reports that as `status=error` with "the module wrote no checks",
    # and `scoring.py:361` promotes that summary -- which carries the last 1500
    # bytes of stdout, i.e. a bare Python traceback through this file -- into the
    # delivered report.  The score is unchanged, because `build.compiles` is
    # required either way.  What the catch buys is a report that distinguishes
    # "did not compile" from "did not finish", and a graded payload with no stack
    # trace pinning this module's paths and line numbers.  Measured both ways in
    # _run/build-timeout-probe.py.
    try:
        proc = subprocess.run(
            ["cargo", "build", "--release", "--offline", "--locked",
             "--target-dir", str(target)],
            cwd=REPO, env=env, capture_output=True, text=True,
            timeout=BUILD_TIMEOUT_SEC)
        rc: int | None = proc.returncode
        log = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        # The partial log is worth keeping: cargo names the crate it was on.
        rc, log = None, _text(exc.stdout) + _text(exc.stderr)
    seconds = time.monotonic() - started
    (WORK / "build.log").write_text(log, encoding="utf-8")

    if rc is None:
        record("build.compiles", False, 8.0,
               f"cargo build --release did not finish within "
               f"{BUILD_TIMEOUT_SEC}s (killed after {seconds:.0f}s)",
               log, required=True)
        flush()
        return 1
    if not record("build.compiles", rc == 0, 8.0,
                  f"cargo build --release exited {rc}",
                  log, required=True):
        flush()
        return 1
    record("build.finishes_in_budget", seconds < BUILD_BUDGET_SEC, 0.5,
           f"the build took {seconds:.0f}s")

    binary = target / "release" / "miniserve"
    if not record("build.produces_binary", binary.is_file(), 4.0,
                  f"no executable at {binary}. The crate has to keep producing "
                  f"a binary named `miniserve`: it is what users invoke.",
                  required=True):
        flush()
        return 1

    # Same catch, and here it protects more: by this line `compiles`,
    # `finishes_in_budget` and `produces_binary` are recorded but only in memory,
    # so an uncaught timeout discards 12.5 of the module's 23.5 weight and makes
    # the report say "wrote no checks" about a tree that provably compiled.
    try:
        version = subprocess.run([str(binary), "--version"], capture_output=True,
                                 text=True, timeout=VERSION_TIMEOUT_SEC)
        vrc: int | None = version.returncode
        vout = (version.stdout or "") + (version.stderr or "")
    except subprocess.TimeoutExpired as exc:
        vrc, vout = None, _text(exc.stdout) + _text(exc.stderr)
    if vrc is None:
        # A `--version` that never returns has a specific shape behind it:
        # argument parsing fell through into starting the server.  Returning here
        # rather than recording and continuing is about the replay, which would
        # pay the same hang 62 times over for a recording of nothing -- and this
        # is the path a failing `build.compiles` already takes, so the thirteen
        # modules that read the recording see nothing new.
        record("build.binary_runs", False, 4.0,
               f"`miniserve --version` did not exit within "
               f"{VERSION_TIMEOUT_SEC}s", vout, required=True)
        flush()
        return 1
    record("build.binary_runs", vrc == 0, 4.0,
           f"`miniserve --version` exited {vrc}", vout, required=True)

    # ------------------------------------------------------------------ replay
    recording: dict = {"sessions": {}, "binary": str(binary),
                       "build_seconds": round(seconds, 1)}
    boot_failures: dict[str, str] = {}
    played = 0
    for position, session in enumerate(corpus.SESSIONS, 1):
        print(f"  {position:2d}/{len(corpus.SESSIONS)} {session.id}", flush=True)
        try:
            recording["sessions"][session.id] = play_session(
                REPO, str(binary), session, WORK / "play" / session.id,
                TREE_SPEC)
            played += 1
        except ServerFailed as exc:
            boot_failures[session.id] = str(exc)[:4000]
            recording["sessions"][session.id] = {"__failed__": str(exc)[:4000]}
        except Exception as exc:                                 # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"[:4000]
            boot_failures[session.id] = detail
            recording["sessions"][session.id] = {"__failed__": detail}
    recording["boot_failures"] = boot_failures

    out = replay.recording_path()
    out.write_text(json.dumps(recording, indent=1, sort_keys=True),
                   encoding="utf-8")
    record("build.recording_published", out.is_file(), 4.0,
           f"no recording written to {out}", required=True)

    # Every session is *also* graded in the surface that owns it, where a boot
    # failure is one finding attributed to the flag it belongs to.  Here it is a
    # single number, so a report opens with "51 of 62 configurations started"
    # rather than with 11 scattered findings.
    record("build.all_sessions_start", not boot_failures, 2.0,
           f"{played} of {len(corpus.SESSIONS)} configurations started; "
           f"could not start: {', '.join(sorted(boot_failures)) or 'none'}",
           json.dumps(boot_failures, indent=1))

    # The only check here that can see an empty recording.
    # `build.recording_published` above asks `out.is_file()` on the line after an
    # unconditional `write_text`, and a session that never booted is stored *in*
    # the recording as `{"__failed__": ...}` -- so it cannot fail while this module
    # reaches it.  Without this check, a binary that compiles, links and answers
    # `--version` but never binds passes every other check in the module: `build`
    # rates 20.5/23.5 and renders as the healthiest of the fourteen modules, while
    # the thirteen that read its recording sit at 0.0000 with nothing in the report
    # saying why.  This check is what puts the reason where the reader is looking.
    # Measured, not reasoned -- see _run/empty-recording-probe.py.
    #
    # `answered > 0` stays a deliberately weak threshold: a partial boot is still
    # a recording, and each failed session is also charged to the surface that
    # owns it.  What this check adds is the one case where stage 2 measured
    # nothing at all, and where the thirteen zeroes are therefore not evidence
    # about the port -- a distinction the report has to carry, because the verdict
    # is the same either way.
    answered = sum(1 for _ in replay.recorded_entries(recording))
    record("build.replay_completed", answered > 0, 1.0,
           f"the replay recorded {answered} responses", required=True)

    shutil.rmtree(WORK / "play", ignore_errors=True)
    flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
