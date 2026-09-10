"""Properties any correct server has, whether or not the baseline is consulted.

Every other module compares against State A case by case.  These do not, and that
is why they are gathered here rather than spread through the surfaces: a check
that passes without measuring parity would inflate a surface's pass rate, and a
surface's rate is meant to answer "how much of *this part* of the migration is
missing".

What they are for is the class of failure a per-case comparison cannot see.  An
ETag that ignores the response entirely collapses a session into one bucket and
compares fine against a baseline whose own validator was marked volatile.  A
hand-rolled range handler that computes the length from the file size instead of
the slice sends a truncated response to every client that trusts the header.  A
moved template that renders ``{{ name }}`` literally.  None of those show up as a
mismatch in one case; all of them show up as a pattern across the recording.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

import replay
from compare import resolve
from harness import corpus


@pytest.fixture(scope="session")
def entries(run):
    """``(session_id, case_id, entry)`` for every case that answered."""
    return list(replay.recorded_entries(run))


def test_no_date_drift_into_body(entries, expected):
    """No body carries a wall-clock date where the baseline's does not.

    The baseline's listings show file mtimes, which the pinned tree makes
    deterministic -- except in the upload sessions, where a file that was just
    created legitimately shows today's date.  So the check is comparative: a
    present-day date is only a finding on a page whose baseline had none.

    What it catches is a port rendering a clock-derived value the baseline does
    not: a "generated at" line, an mtime read as *now* because the port stats the
    wrong thing, a cache-busting query string built from the current time.  Each
    makes the page ungradeable for reasons that have nothing to do with the port's
    correctness elsewhere.
    """
    # This year and next, rather than a fixed floor: a task graded years from now
    # should still ask "is this today's date" and not "is this after 2026".  The
    # sample tree's own mtimes are pinned to 2021.
    years = (date.today().year, date.today().year + 1)
    suspicious = re.compile("|".join(rf"{y}-\d{{2}}-\d{{2}}" for y in years))
    hits = []
    for session_id, case_id, entry in entries:
        body = entry.get("body")
        if not isinstance(body, str) or not suspicious.search(body):
            continue
        want = expected(session_id, case_id) or {}
        # The capture marked this body volatile, which is a statement about the
        # baseline rather than about the calendar: the page moved between two runs
        # of State A minutes apart, so a live value is expected here.  Preferred
        # over searching the recorded body for a present-day date, because it was
        # recorded once and may be graded in a later year.
        if "body" in (want.get("volatile") or ()):
            continue
        hits.append(f"{session_id}::{case_id}")
    assert not hits, ("bodies carry a present-day date where the baseline's do "
                      f"not, so they cannot be compared: {hits[:10]}")


def test_etags_distinguish_by_size(run):
    """Backstop: two responses of different lengths never share an ETag.

    The stronger claim -- every distinct file gets its own validator -- is not
    gradeable here, and it is worth being explicit about why.  actix-files builds
    its ETag from inode, size and mtime; the harness scrubs the inode, since that
    number is assigned by whichever filesystem the grader runs on, and every
    sample mtime is pinned to make the rest reproducible.  What survives is
    ``INO:size:pinned``, so two same-sized files arrive with byte-identical
    recorded ETags.  Asserting per-file uniqueness would be asserting a property
    of the scrubber.

    Real coverage of the ETag lives in the per-case comparison, where 123
    responses carry one and its presence is pinned by the header-set check.  This
    is kept for the failure it names clearly: an ETag that ignores the response
    collapses a session into one bucket, and reading "one ETag is serving 14- and
    4200-byte bodies" beats reading a diff of two opaque quoted strings.
    """
    problems = []
    for session_id, session in (run.get("sessions") or {}).items():
        if not isinstance(session, dict) or "__failed__" in session:
            continue
        by_tag: dict[str, dict[int, str]] = {}
        for case_id, entry in session.items():
            if case_id.startswith("__") or not isinstance(entry, dict):
                continue
            # The header map keeps every value a header arrived with, so this is a
            # list.  Joined rather than indexed: a port that sends two ETags is
            # already wrong and its pair should not collapse onto one of them.
            values = (entry.get("headers") or {}).get("etag")
            tag = " ".join(values) if isinstance(values, list) else values
            length = entry.get("body_len")
            if not tag or entry.get("status") != 200 or not length:
                continue
            by_tag.setdefault(tag, {}).setdefault(length, case_id)
        for tag, sizes in by_tag.items():
            if len(sizes) > 1:
                shown = ", ".join(f"{case} ({size} bytes)"
                                  for size, case in sorted(sizes.items())[:4])
                problems.append(f"  {session_id}: {tag} -> {shown}")
    assert not problems, (
        "one ETag is serving responses of different lengths, so it cannot be "
        "derived from what was sent:\n" + "\n".join(problems[:12]))


def test_content_length_matches_body(entries):
    """A declared ``Content-Length`` agrees with the bytes actually sent.

    Not a parity check, and the reason ``content-length`` is worth recording even
    though it is not compared case by case.  A range handler that computes the
    length from the file size instead of the slice sends a truncated-looking
    response to every client that trusts the header, and no comparison against
    State A would notice if State A happened to be volatile there.
    """
    wrong = []
    for session_id, case_id, entry in entries:
        declared = entry.get("declared_length")
        if declared is None or entry.get("chunked"):
            continue
        if declared == -1:
            wrong.append(f"{session_id}::{case_id}: content-length is present "
                         f"but not a number")
            continue
        # HEAD and 304 declare a length and send no body, by design.
        if entry.get("raw_len") == 0 and declared > 0:
            continue
        if declared != entry.get("raw_len"):
            wrong.append(f"{session_id}::{case_id}: declared content-length "
                         f"{declared} but {entry.get('raw_len')} bytes arrived")
    assert not wrong, "\n".join(wrong[:20])


def test_no_template_leakage(entries):
    """No unrendered template syntax, and no Rust debug output, on any page.

    The baseline passes this trivially, which is the point: it is a specific and
    common porting failure rather than a difference.  A moved template that
    renders ``{{ name }}`` literally, or an error path that formats a ``Debug``
    impl into the page and ships ``Custom { kind: NotFound, error: ... }`` to the
    browser.

    Only the OPENING delimiters are searched for, and the baseline is why.  ``}}``
    and ``%}`` both occur in miniserve's own minified stylesheet -- ``}}`` closes a
    rule nested inside a media query, ``%}`` ends a declaration whose value is a
    percentage -- so the closing halves matched nine ``text/css`` responses on a
    tree that leaks nothing, while no opening delimiter matched anything.  Dropping
    them costs no detection: unrendered template syntax always shows the delimiter
    that opens it, and a body containing ``{{`` is what a literal ``{{ name }}``
    looks like.  Searching for the closing half alone only finds punctuation.

    Scoping by content-type would have silenced the same nine cases, and is the
    worse fix: the stylesheet is itself generated per boot, so it is a place a leak
    could actually appear, and this is the only check that would see it.
    """
    needles = ("{{", "{%", "Err(", "Error {", "kind:",
               "thread 'main' panicked", "RUST_BACKTRACE")
    hits = []
    for session_id, case_id, entry in entries:
        if entry.get("status") == 0 or not entry.get("is_text"):
            continue
        body = entry.get("body") or ""
        for needle in needles:
            if needle in body:
                hits.append(f"  {session_id}::{case_id}: contains {needle!r}")
                break
    assert not hits, ("template or debug output is leaking into response "
                      "bodies:\n" + "\n".join(hits[:20]))


def test_archive_members_are_relative(entries):
    """No absolute paths and no ``..`` inside any archive.

    A genuine security property of the endpoint rather than a parity check, and
    one a port loses by handing the archiver an absolute filesystem path instead
    of a path relative to the served root.  An archive containing
    ``/home/user/files/x`` or ``../../etc/passwd`` unpacks outside the directory
    the user extracted it into.
    """
    hits = []
    for session_id, case_id, entry in entries:
        if resolve(entry, "archive.ok") is not True:
            continue
        members = resolve(entry, "archive.members")
        if not isinstance(members, list):
            continue
        for member in members:
            name = member.get("name") if isinstance(member, dict) else None
            if not isinstance(name, str):
                continue
            if name.startswith("/"):
                hits.append(f"  {session_id}::{case_id}: {name!r} is absolute")
            elif ".." in Path(name).parts:
                hits.append(f"  {session_id}::{case_id}: {name!r} escapes the "
                            f"archive root")
    assert not hits, ("archive members would unpack outside the extraction "
                      "directory:\n" + "\n".join(hits[:20]))


def test_transport_failures_match(actual, expected):
    """Where the baseline hung up, the port hangs up -- and the reverse.

    Recorded as a status of 0 with a transport marker.  Called out separately
    because it is the one failure mode that looks like a harness problem when it
    is not: actix-web closes the connection without answering for some malformed
    request lines, and a port that answers 400 instead is making a different,
    defensible choice that is nonetheless a difference a client sees.
    """
    problems = []
    for session, case in corpus.all_cases():
        got, want = actual(session.id, case.id), expected(session.id, case.id)
        if got is None or want is None:
            continue
        got_hung, want_hung = got.get("status") == 0, want.get("status") == 0
        if got_hung != want_hung:
            problems.append(
                f"  {session.id}::{case.id} ({case.path!r}): baseline "
                + ("closed the connection" if want_hung
                   else f"answered {want.get('status')}")
                + ", submission "
                + ("closed the connection" if got_hung
                   else f"answered {got.get('status')}"))
    assert not problems, ("connection-level behaviour differs:\n"
                          + "\n".join(problems[:20]))


def test_every_case_answered(run, actual, expected):
    """Nothing in the corpus went unrecorded in a session that started.

    The check that keeps every other module honest.  A recording missing a case
    makes that case's comparisons fail with "no record", which reads like a
    submission fault; gathered here it reads as what it is -- the server stopped
    answering partway through a session that did boot.

    Sessions that never booted are excluded, because that is a different finding
    and it already has an owner: the surface whose flag the session exercises
    reports it, and the build module reports the count.
    """
    booted = set(replay.boot_failures(run))
    missing = []
    for session, case in corpus.all_cases():
        if session.id in booted:
            continue
        if expected(session.id, case.id) is None:
            continue                      # not recorded in State A either
        if actual(session.id, case.id) is None:
            missing.append(f"{session.id}::{case.id}")
    assert not missing, (
        f"{len(missing)} requests went unanswered in sessions that started, so "
        f"the server stopped partway through: {missing[:15]}")
