"""Ask the submission every recorded request once, in order, and write it down.

This module produces no comparisons. It produces the file the four comparison
modules read: ``$SRB_SUITE_WORK/replay.json``, the submission's answers to exactly
the requests State A was asked.

Why it is one module and not four
---------------------------------
json-server is stateful. A POST changes what the next GET sees, so the order of
the requests is part of the ground truth, and a session that mutates cannot share
a server with anything else. That makes a replay expensive: sixteen server boots,
each with a freshly seeded database.

Doing it inside each comparison module would mean four replays -- four times the
cost, and worse, four different recordings. `read` would compare statuses against
one run and `write` bodies against another, so a submission that is intermittently
wrong could pass every module while never once being right. Here there is one
recording, and every assertion anywhere in the stage describes a response that
actually happened.

What it is scored on
--------------------
One check per session, and they are real checks rather than bookkeeping: that the
server started at all under that configuration, that it answered every request in
the session, and that its closing read answered what State A's closing read
answered -- a mutating session that kills the server on its last write would
otherwise look complete.

That last one is compared against the recording rather than against 200. Three
sessions delete post 1 and then ask for it, so State A itself answers 404 there.

A session that fails to start is reported here, once, with its log. The comparison
modules then report their own cases as failures without a stack trace each, which
is the difference between a readable result and three hundred copies of the same
message.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(os.environ["SRB_SUITE_DIR"]) / "lib"))

from harness import exchanges                                    # noqa: E402
from harness.runner import ServerFailed, play_session, pin_static_mtimes  # noqa: E402

SUITE = Path(os.environ["SRB_SUITE_DIR"])
DATA = SUITE / "data"
SHARED = Path(os.environ["SRB_SUITE_WORK"])
WORK = Path(os.environ.get("SRB_WORK", "/tmp/srb"))
RESULT = Path(os.environ["SRB_RESULT"])
LAUNCHER = os.environ.get("SRB_LAUNCHER", "serve.sh")

BUILD = SHARED / "build"
OUT = SHARED / "replay.json"
SEED = DATA / "db.seed.json"
ROUTES = DATA / "routes.json"
RECORDED = DATA / "recorded-responses.json"

CHECKS: list[dict] = []


#: The three below are the Node-crash branch of ``swerefactor.pytest_module``'s rule:
#: a continuation that is only a location (``file:///app/src/cli/index.js:10``)
#: stands in for the exception printed four lines under it.  It does not fire on
#: anything *this* module writes -- its details lead with a sentence ending in the
#: exception, not in a colon -- and it is here anyway, because the docstring below
#: claims this is the same rule as infra's, and a copy that quietly stops being one
#: is how the next reader gets misled.
_BARE_LOCATION = re.compile(r"^\S+:\d+(?::\d+)?$")
_CAUSE_LINE = re.compile(r"^[A-Za-z_][\w.]*: \S")
_CAUSE_WINDOW = 6


def headline(detail: str, limit: int = 300) -> str:
    """The first line of ``detail``, plus the second when the first only announces.

    A module that writes its own checks has to fill ``summary`` itself; pytest
    modules get it from ``swerefactor.pytest_module._headline`` and this is the same
    rule, kept here rather than imported because a module is standalone by contract.

    It was missing entirely, and the cost was concrete: on fw02's round-3 submission
    all 16 of this module's failing checks reached the artifact with an empty
    headline and the whole finding in ``detail``, where the stage-2 report never
    looks -- the report prints the module table and no check text at all.  The line
    this function returns is the one ``record`` was already computing for stdout and
    then throwing away.
    """
    lines = [ln for ln in (detail or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    head = lines[0].strip()
    if head.endswith(":") and len(lines) > 1:
        cont = lines[1].strip()
        if _BARE_LOCATION.match(cont):
            for ln in lines[2:2 + _CAUSE_WINDOW]:
                if _CAUSE_LINE.match(ln.strip()):
                    cont = f"{ln.strip()} (at {cont})"
                    break
        head = f"{head} {cont}"
    return head[:limit]


def record(check_id: str, verdict: str, detail: str, **extra) -> None:
    entry = {"id": check_id, "verdict": verdict,
             "summary": headline(detail), "detail": detail}
    entry.update(extra)
    CHECKS.append(entry)
    print(f"[{verdict:5s}] {check_id}: {detail.splitlines()[0][:160]}", flush=True)


def finish() -> None:
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps({"checks": CHECKS}, indent=1))
    bad = [c["id"] for c in CHECKS if c["verdict"] in ("fail", "error")]
    print(f"\n{len(CHECKS)} checks, {len(bad)} not passing"
          + (f": {', '.join(bad)}" if bad else ""))
    raise SystemExit(0)


def static_epoch(recorded: dict) -> float | None:
    """The mtime State A's static files had when the recording was made.

    Express derives a static file's ETag and Last-Modified from its size and its
    mtime, so a correct submission serving a byte-identical file still answers a
    different validator if the file's timestamp moved -- and a timestamp moves
    for reasons that have nothing to do with the migration, such as how the tree
    was checked out. Pinning it from the recording's own Last-Modified keeps the
    conditional-request cases reproducible; the size is left alone, so a
    submission that actually edited a static file still fails.
    """
    import email.utils
    entry = recorded.get("responses", {}).get("read", {}).get("static-index")
    values = (entry or {}).get("headers", {}).get("last-modified") or []
    if not values:
        return None
    try:
        return email.utils.parsedate_to_datetime(values[0]).timestamp()
    except (TypeError, ValueError):
        return None


def main() -> None:
    if not (BUILD / "package.json").is_file():
        record("build-present", "fail",
               f"nothing installed at {BUILD}: the install module did not "
               f"finish, so there is no server to replay against.")
        OUT.write_text(json.dumps({"sessions": {}, "install_missing": True}))
        finish()

    # The request list and the recording have to describe the same requests, or
    # the comparison modules would be diffing answers to different questions.
    # Checked here, once, because this is the module that reads both.
    stored = json.loads(RECORDED.read_text())
    current = exchanges.fingerprint()
    if stored.get("exchanges_fingerprint") != current:
        record("fingerprint", "error",
               f"the recording was made from request list "
               f"{stored.get('exchanges_fingerprint')}, this harness is "
               f"{current}. Nothing can be compared. Re-record with "
               f"`python -m harness.capture`.")
        finish()
    record("fingerprint", "pass",
           f"request list {current} matches the recording "
           f"({stored['case_count']} exchanges in {stored['session_count']} "
           f"sessions)")

    epoch = static_epoch(stored)
    if epoch is not None:
        pinned = pin_static_mtimes(BUILD, epoch)
        print(f"    pinned mtime on {len(pinned)} static file(s)", flush=True)

    workdir = WORK / "replay"
    workdir.mkdir(parents=True, exist_ok=True)

    sessions: dict[str, dict] = {}
    for session in exchanges.SESSIONS:
        started = time.monotonic()
        label = f"session/{session.id}"
        # Always the one seed. The two sessions that need a differently shaped
        # database (`--id _id`, `--foreignKeySuffix _id`) name a transform, which
        # the runner applies to this file when it writes the session's own copy --
        # so the variant is derived here exactly as it was at capture time rather
        # than being a second data file that has to be kept in step.
        try:
            answers = play_session(BUILD, LAUNCHER, session, workdir,
                                   SEED, ROUTES)
        except ServerFailed as exc:
            sessions[session.id] = {"__failed__": f"{exc}\n\n{exc.log}"}
            record(label, "fail",
                   f"the server did not run this session "
                   f"(argv {list(session.argv) or 'none'}): {exc}\n\n{exc.log}",
                   argv=list(session.argv))
            continue
        except Exception as exc:                          # noqa: BLE001
            sessions[session.id] = {"__failed__": repr(exc)}
            record(label, "error",
                   f"replaying this session raised {type(exc).__name__}: {exc}",
                   argv=list(session.argv))
            continue

        elapsed = time.monotonic() - started
        expected_ids = [c.id for c in session.cases]
        missing = [cid for cid in expected_ids if cid not in answers]
        alive = answers.get("__alive__", {})

        # The session ends with a plain GET /posts/1, to prove the server
        # outlived its own last write. What that read *answers* is not always
        # 200: `delete`, `override` and `custom-fk` remove post 1 before it is
        # asked for, and State A answered 404. Requiring a literal 200 would
        # therefore assert that those three deletes had not worked.
        #
        # Comparing against the recording is the stricter check, not the looser
        # one. A server that died answers nothing at all, which still fails. A
        # server that answers 200 where State A answered 404 kept a record it
        # was told to delete, and a fixed 200 would have called that a pass.
        recorded_alive = (stored.get("responses", {})
                          .get(session.id, {})
                          .get("__alive__") or {})
        want = recorded_alive.get("status")
        got = alive.get("status")

        if missing:
            record(label, "fail",
                   f"{len(missing)} of {len(expected_ids)} request(s) produced no "
                   f"answer: {', '.join(missing[:6])}",
                   argv=list(session.argv))
        elif want is None:
            record(label, "error",
                   f"the recording has no final read for this session, so there "
                   f"is nothing to compare the server's survival against. "
                   f"Re-record with `python -m harness.capture`.",
                   argv=list(session.argv))
        elif got is None:
            record(label, "fail",
                   f"all {len(expected_ids)} requests answered, but the server "
                   f"was no longer serving afterwards: the closing GET /posts/1 "
                   f"produced no response at all. A session that dies on its "
                   f"last write leaves the earlier answers unusable.",
                   argv=list(session.argv))
        elif got != want:
            record(label, "fail",
                   f"all {len(expected_ids)} requests answered and the server "
                   f"was still up, but the closing GET /posts/1 returned {got} "
                   f"where State A returned {want}. The session leaves the "
                   f"database in a different state than it recorded.",
                   argv=list(session.argv))
        else:
            record(label, "pass",
                   f"{len(expected_ids)} request(s) answered in order in "
                   f"{elapsed:.1f}s; server still serving afterwards "
                   f"(closing read {got}, as recorded)",
                   argv=list(session.argv))

        sessions[session.id] = answers

    OUT.write_text(json.dumps({
        "schema": "swerefactor.fw02-replay/1",
        "exchanges_fingerprint": current,
        "sessions": sessions,
    }, indent=1, sort_keys=True))
    print(f"\nwrote {OUT} ({OUT.stat().st_size} bytes)")
    finish()


if __name__ == "__main__":
    sys.exit(main())
