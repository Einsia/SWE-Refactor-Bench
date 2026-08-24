"""Replay the corpus against the submission, recording it exactly as capture did.

This is the grading-time counterpart of :mod:`harness.capture`, and the two are
deliberately thin wrappers around the *same* three modules: :mod:`harness.launch`
starts the process, :mod:`harness.wire` turns a case into bytes, and
:mod:`harness.normalize` turns a response into a record.  Nothing about how a
request is formed or how a response is reduced lives here, because anything that
did would apply to only one side of the comparison.

The one asymmetry is intentional and is the reason this module exists at all:
**capture runs each profile twice and keeps the intersection; grading runs each
profile once.**  Two runs are how volatility is discovered, and volatility is a
property of State A that was settled at construction time.  Running the
submission twice would not add information -- a field already known to be stable
in State A either matches or does not -- and would double an already long
grading run.

A profile that fails to launch does not abort the session.  It yields a
``__failed__`` sentinel, every test for that profile fails with the process
output attached, and the other 55 profiles still produce a score.  A submission
that panics under ``--enable-metrics`` should lose the metrics profiles, not the
whole grade.
"""

from __future__ import annotations

import os

from harness import normalize, wire
from harness.launch import LaunchError, serve


def play_profile(binary: str, profile, *, rundir: str) -> dict:
    """Launch ``binary`` for one profile and replay its cases in order.

    Mirrors :func:`harness.capture.capture_profile` line for line.  Case order is
    preserved because it is part of the contract for any profile that writes: a
    push, a re-push and a delete only mean something in sequence.
    """
    os.makedirs(rundir, exist_ok=True)
    out: dict = {"id": profile.id, "cases": {}}
    server = serve(binary, flags=profile.flags, seeds=profile.seeds,
                   rundir=rundir, tls=profile.tls)
    try:
        out["argv"] = list(server.argv)
        for case in profile.cases:
            status, headers, body = wire.send(server, case)
            out["cases"][case.id] = normalize.record(
                case.id, status, headers, body,
                body_mode=case.body_mode, method=case.method,
                prefixes=(case.prefix_len,) if case.prefix_len else (),
            )
    finally:
        server.stop()
        out["log"] = server.log[-8000:]
    return out


def play_all(binary: str, profiles, workdir: str, *, progress=None) -> dict:
    """Replay every profile.  Returns ``{profile_id: record}``.

    ``record`` is either a normal recording or ``{"__failed__": str}``.  A launch
    failure is caught per profile rather than allowed to propagate, for the reason
    in the module docstring.

    ``workdir`` must sit under ``/tmp``, and that is a real constraint rather than
    a tidiness preference.  A storage-layer error body embeds the absolute object
    path -- 12 recorded cases say ``remove /tmp/PATH: no such file or directory``
    -- and ``normalize.scrub_text`` collapses ``/tmp/...`` and ``/workspace/...``
    to *different* placeholders.  Capture ran from ``tempfile.mkdtemp()``, so the
    frozen expectation is the ``/tmp`` one; grading from ``/workspace`` would
    record ``/PATH`` and fail those cases for a reason having nothing to do with
    the submission.  Checked rather than documented, because the failure it
    produces looks like a genuine behavioural difference.
    """
    resolved = os.path.realpath(workdir)
    if not (resolved == "/tmp" or resolved.startswith("/tmp/")):
        raise ValueError(
            f"replay workdir must be under /tmp, got {resolved!r}: storage paths "
            "appear in recorded error bodies and are scrubbed per-prefix, so the "
            "frozen expectations only match a /tmp rundir (see docstring)")
    out: dict[str, dict] = {}
    for i, profile in enumerate(profiles, 1):
        rundir = os.path.join(workdir, profile.id)
        try:
            out[profile.id] = play_profile(binary, profile, rundir=rundir)
        except LaunchError as exc:
            out[profile.id] = {"__failed__": str(exc), "id": profile.id,
                               "cases": {}}
        except OSError as exc:
            # A missing binary, an unwritable rundir: infrastructure rather than
            # submission behaviour, but it must still be attributable to a
            # profile rather than crash collection.
            out[profile.id] = {"__failed__": f"{type(exc).__name__}: {exc}",
                               "id": profile.id, "cases": {}}
        if progress:
            progress(i, len(profiles), profile.id, out[profile.id])
    return out
