"""Loading the two recordings the comparison modules read.

Thirteen of this suite's sixteen modules never touch the submission.  They open
two JSON documents -- the frozen State A capture that ships in ``data/``, and the
recording the ``build`` module made of the submission -- and compare them.  That
is not a shortcut, it is the property that makes the suite hard to game:

*   **The submission cannot tell which test is looking at it.**  Every request it
    will ever answer arrives during one replay, in one order, before any
    comparison happens.  There is no observable difference between the run that
    grades archives and the run that grades auth, so behaviour cannot be varied
    per test.
*   **It is measured once.**  Sixteen modules booting 62 servers each would be 992
    boots and an hour of wall clock, and would let a flaky port score differently
    in each module.
*   **A module cannot accidentally read the tree.**  There is no ``repo`` fixture
    here.  A module that wanted to grep the submission's source would have to
    construct the path itself, which is visible in review -- and the question it
    would be asking belongs to stage 1, which reads both trees and executes
    neither.

The three exceptions declare themselves: ``build`` (which produces the
recording), ``closure`` (which asks cargo what it resolved) and ``differential``
(which boots the built binary again with inputs the corpus never used).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path

from harness import corpus

#: The frozen State A capture.  Shipped gzipped: it is 8.1 MB of JSON that no
#: reviewer reads by eye and that git would store twice on every edit.
GOLDEN = Path(os.environ.get(
    "SRB_GOLDEN", "/tests/behavioural/data/responses.json.gz"))

#: Where the build module publishes what the submission answered.
def recording_path() -> Path:
    shared = os.environ.get("SRB_SUITE_WORK")
    if not shared:
        raise RuntimeError(
            "SRB_SUITE_WORK is unset; this module must be run by the suite "
            "runner, which is what creates the shared scratch directory")
    return Path(shared) / "actual.json"


class MissingRecording(RuntimeError):
    """The build module did not publish a recording."""


#: Parsed recordings, keyed by resolved path.  The golden is 8.1 MB of JSON and
#: each module process reads it at least twice -- once while collecting, to
#: compute which cases carry an html record, and once from the ``golden``
#: fixture -- so it is parsed once and shared.  Callers treat the returned
#: document as read-only.
_PARSED: dict[Path, dict] = {}


def load_golden(path: Path | None = None) -> dict:
    """The State A capture, with its fingerprint checked against the corpus.

    The fingerprint covers the corpus definition -- every session's argv and every
    case's request.  If someone edits ``harness/corpus.py`` without re-capturing,
    the two are describing different request sets and every comparison after this
    point would be nonsense, so it fails here with the reason instead.
    """
    path = Path(path or GOLDEN).resolve()
    if path in _PARSED:
        return _PARSED[path]
    if not path.is_file():
        raise MissingRecording(f"the State A capture is missing: {path}")
    raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" \
        else path.read_bytes()
    data = json.loads(raw)
    want = corpus.fingerprint()
    if data.get("fingerprint") != want:
        raise RuntimeError(
            f"{path} was captured against a different corpus "
            f"({data.get('fingerprint', '?')[:16]} != {want[:16]}). The recorded "
            f"answers do not describe the requests this suite sends, so nothing "
            f"here can be compared. Re-capture, or restore harness/corpus.py.")
    data["digest"] = hashlib.sha256(raw).hexdigest()
    _PARSED[path] = data
    return data


def load_actual(path: Path | None = None) -> dict:
    """What the submission answered, as published by the ``build`` module."""
    path = (Path(path) if path else recording_path()).resolve()
    if path in _PARSED:
        return _PARSED[path]
    if not path.is_file():
        raise MissingRecording(
            f"no recording of the submission at {path}. The build module runs "
            f"first and publishes it; if that module failed, every module after "
            f"it has nothing to compare and reports so rather than passing.")
    data = json.loads(path.read_text(encoding="utf-8"))
    _PARSED[path] = data
    return data


def lookup(recording: dict, *, golden: bool):
    """``(session_id, case_id) -> entry or None``.

    The two documents nest differently -- the golden keeps its cases under a
    ``cases`` key beside the session's argv, the live recording is the bare case
    map -- and both shapes are load-bearing, so this hides the difference rather
    than rewriting either.
    """
    sessions = recording.get("sessions") or {}

    def get(session_id: str, case_id: str):
        session = sessions.get(session_id)
        if not session:
            return None
        if golden:
            return (session.get("cases") or {}).get(case_id)
        if "__failed__" in session:
            return None
        return session.get(case_id)

    return get


def boot_failures(recording: dict) -> dict[str, str]:
    """Sessions the submission could not be started for."""
    explicit = recording.get("boot_failures")
    if isinstance(explicit, dict):
        return explicit
    return {sid: session["__failed__"]
            for sid, session in (recording.get("sessions") or {}).items()
            if isinstance(session, dict) and "__failed__" in session}


def golden_carries(field: str, cases, *, golden: dict | None = None) -> list:
    """Filter ``(session, case)`` pairs down to those State A has ``field`` for.

    The battery axes are keyed on this rather than on ``case.body_mode``, and the
    difference is not cosmetic.  It cuts both ways.

    Too wide: 429 cases declare ``html`` or ``shape``, but only 389 of them have a
    parsed html record.  The other 40 are raw or HEAD requests whose shape
    comparison is over headers alone.  Parametrising the ~20 html field checks by
    ``body_mode`` would run 800 comparisons that read ``MISSING`` on both sides and
    pass for free, lifting the module's pass rate without measuring anything.  A
    miss has to be able to happen for a pass to mean something.

    Too narrow: 27 further cases declare ``exact`` or ``archive`` -- their bodies
    are graded byte for byte, or as an unpacked member list -- and *also* carry a
    parsed html record, because the response was a page either way.  Keying on
    ``body_mode`` would leave those 27 pages ungraded as pages.  The axis holds 416
    cases, which is neither 389 nor 429.
    """
    data = golden if golden is not None else load_golden()
    get = lookup(data, golden=True)
    out = []
    for session, case in cases:
        entry = get(session.id, case.id)
        if isinstance(entry, dict) and isinstance(entry.get(field), dict):
            out.append((session, case))
    return out


def recorded_entries(recording: dict):
    """``(session_id, case_id, entry)`` for every real case that answered."""
    for session_id, session in (recording.get("sessions") or {}).items():
        if not isinstance(session, dict) or "__failed__" in session:
            continue
        for case_id, entry in session.items():
            if case_id.startswith("__") or not isinstance(entry, dict):
                continue
            yield session_id, case_id, entry
