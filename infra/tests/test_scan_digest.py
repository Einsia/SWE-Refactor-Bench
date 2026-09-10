"""The channel that carries a scan's findings into the reviewer's prompt.

This is the only path by which a mechanical observation reaches the model that
decides a stage-1 gate, and its budget is small: one summary line and 240
characters of detail per finding.  What fits in that window is therefore a design
question, not a formatting one -- a caveat that does not fit has not been said.

Measured on fw06: a scan whose report said "these counts are a LOWER BOUND"
delivered "... without: ['/'] E 1 furt" to the reviewer, and the reviewer read the
counts as facts.  The tests here are about what survives.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor.result import Check
from swerefactor.scan import _message, digest

FRAME = ("modules/dispatch/test_dispatch.py:116: in "
         "test_report_the_registration_table\n    pytest.fail(\n")


def pytest_detail(*message_lines: str) -> str:
    return FRAME + "".join(f"E   {ln}\n" for ln in message_lines)


# --------------------------------------------------------------------------- #
# what the 240 characters get spent on
# --------------------------------------------------------------------------- #


def test_the_pytest_frame_is_not_charged_to_the_window():
    """83 characters of the 240 restated the check id printed above them."""
    detail = pytest_detail("Failed: REPORT (not a defect): 4 registration(s)",
                           "  with a method: []")
    got = _message(detail)
    assert "test_dispatch.py:116" not in got
    assert "pytest.fail(" not in got
    assert got.startswith("Failed: REPORT (not a defect): 4 registration(s)")


def test_the_headline_is_not_printed_twice():
    """``summary`` is the first E line, so the detail must start after it."""
    head = "Failed: REPORT (not a defect): 4 registration(s)"
    detail = pytest_detail(head, "  with a method: []", "  without: ['/']")
    got = _message(detail, already_said=head)
    assert not got.startswith(head)
    assert got.startswith("with a method: []")


def test_a_caveat_that_leads_survives_the_window():
    """The whole point: the correction arrives, not its first three words.

    Transcribed from fw06's registration report after the ordering fix.  The
    warning is the first sentence of the message, so it is what the summary
    carries and what the detail's window opens onto.
    """
    warning = ("Failed: REPORT (not a defect): 1 line(s) build a pattern at "
               "RUNTIME and may register many, so every count below is a LOWER "
               "BOUND, not a fact.")
    detail = pytest_detail(warning,
                           "  Among the 1 readable literal(s), 0 carry a method "
                           "prefix and 1 do not; 4 registration line(s) in total.",
                           *[f"  pkg/server.go:{i}: .Handle /x{i}" for i in range(40)])
    check = Check(id="dispatch/test_report_the_registration_table", verdict="fail",
                  summary=warning, detail=detail)
    text = digest([check])
    assert "LOWER BOUND, not a fact." in text
    # And the counts it corrects are still there, after it rather than before.
    assert "0 carry a method prefix" in text
    assert text.index("LOWER BOUND") < text.index("0 carry a method prefix")


def test_a_detail_with_no_e_block_is_left_alone():
    """A verifier's own prose has no frame to drop."""
    prose = ("[fail] The process entry point wires the HTTP server directly to "
             "dispatch.NewRouter, and all five registrations use the copied "
             "router's fluent API.")
    assert _message(prose) == prose


def test_an_elision_marker_is_kept():
    detail = (FRAME + "E   Failed: REPORT: 900 registration(s)\n"
              "  ... [4000 character(s) elided] ...\n"
              "E     pkg/server.go:9001: .Handle /\n")
    got = _message(detail)
    assert "elided" in got


# --------------------------------------------------------------------------- #
# the digest's own contract
# --------------------------------------------------------------------------- #


def test_findings_are_leads_and_the_digest_says_so():
    check = Check(id="dispatch/x", verdict="fail", summary="something",
                  detail=pytest_detail("Failed: something"))
    text = digest([check])
    assert "not a finding you may report as your own" in text


def test_passes_are_counted_not_listed():
    checks = [Check(id=f"m/c{i}", verdict="pass", summary="") for i in range(30)]
    checks.append(Check(id="m/flag", verdict="fail", summary="found it",
                        detail=pytest_detail("Failed: found it")))
    text = digest(checks)
    assert "30 found nothing, 1 found something" in text
    assert "m/c0" not in text
    assert "m/flag" in text


def test_nothing_flagged_is_not_a_verdict_on_the_gates():
    text = digest([Check(id="m/c", verdict="pass", summary="")])
    assert "Nothing was flagged" in text
    assert "not a verdict on the gates" in text


def test_an_empty_scan_says_so_rather_than_rendering_blank():
    assert digest([]) == "(the scan produced no findings)"
