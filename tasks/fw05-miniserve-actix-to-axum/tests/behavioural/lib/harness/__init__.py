"""Capture and replay harness for the miniserve port.

The same code drives the golden capture against State A and the graded run
against the submission, so a difference in the recorded response can only come
from the server. Nothing in here is specific to actix-web or to axum.

Layout:

*   ``sessions``  -- the data model: what a session and a case are.
*   ``tree``   -- the grader's own tree builder, plus ``tree_digest``.
*   ``normalize`` -- the five classes of legitimate per-boot variation.
*   ``corpus``    -- the sessions themselves: what is actually measured.
*   ``runner``    -- boots a process per session and replays it.
*   ``capture``   -- writes the golden file; run twice, at image build time.
"""

from .sessions import BOUNDARY, PORT_BASE, Case, Session
from .corpus import SESSIONS, all_cases, case_key, fingerprint, summary

__all__ = [
    "BOUNDARY", "PORT_BASE", "Case", "Session",
    "SESSIONS", "all_cases", "case_key", "fingerprint", "summary",
]
