"""What the pytest plugin writes into a module's payload.

``pytest_module`` decides three things no other file decides: what counts as a
scored check, what a skip means, and what one sentence a reviewer reads for each
finding.  It had no tests, and a bug lived in each of the last two.

The skip licence: an unlicensed skip is scored as a miss, and a miss costs its
module credit.  A check that costs credit has to say why in the payload a reviewer
reads, so these tests assert on the recorded prose and not only on the verdict.

The headline: `_summary` walked pytest's ``E`` block backwards, so every headline
was pytest's rewritten restatement of the assertion rather than the sentence the
check's author wrote -- and it returns the truthy word "skipped" for a skip, so
an `or` fallback could never supply a missing explanation either.  Two branches
found that independently and measured it the same way from opposite ends: on one
fw05 run's 47 behavioural failures the last ``E`` line was unusable in 22 of them,
20 of those being literally ``]``, and the first in none.

Two styles of test here, and the split is deliberate.  Most of the file drives
real pytest in a subprocess and reads the JSON back: the plugin writes its
payload in `pytest_unconfigure`, so the file only exists once the inner session
has fully torn down, and fabricating a ``longrepr`` would be the same mistake in
a different place -- the bug was a wrong belief about what pytest writes and in
which order, and only pytest can settle that.  The last section calls the
reduction helpers directly on blocks shaped like the ones this suite emits.  That
is sound *because* of the sections above it: they establish the shape, so a
fabricated block of that shape is a fixture rather than a guess, and it can reach
arrangements a generated module cannot easily produce -- ``compare.py``'s mapping
dump, a block with no ``E`` lines at all, a detail 5000 characters long.

The two fields are not interchangeable, and a third branch's tests are appended
here on that basis.  ``summary`` is the line a reader sees before deciding whether
to read further: ``scan.digest`` puts it beside the check id, ``cli`` prints 90
characters of it per gate.  ``detail`` is the body, and every consumer of it reads
the FRONT -- 240 characters into the reviewer's prompt, the first six lines into
report.txt, 2000 into score.json -- which is why it is trimmed from the middle and
not from either end.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(INFRA))

from swerefactor.pytest_module import (DETAIL_HEAD, DETAIL_LIMIT, _Recorder,
                                     _detail, _summary)
# The consumer that strips a detail's repeated headline.  Imported here because the
# last section asserts that half of the fix at the layer it was landed on; see the
# banner above it for why that is not this module's job.
from swerefactor.scan import _detail_beyond_summary


# --------------------------------------------------------------------------
# one module, read end to end: every verdict, and the prose
# --------------------------------------------------------------------------

MODULE = '''
import pytest

def test_passes():
    assert True

def test_fails():
    assert 1 == 2, "a real assertion"

@pytest.mark.skip(reason="the jar was never produced")
def test_unlicensed_skip():
    pass

@pytest.mark.skip
def test_unlicensed_skip_bare():
    pass

@pytest.mark.skip(reason="no module-info.class in the jar")
def test_unlicensed_skip_missing_artifact():
    pass

@pytest.mark.srb_skip_ok
@pytest.mark.skip(reason="the optional macro was not implemented")
def test_licensed_skip():
    pass
'''


@pytest.fixture(scope="module")
def payload(tmp_path_factory) -> dict:
    work = tmp_path_factory.mktemp("mod")
    (work / "test_mod.py").write_text(MODULE)
    out = work / "result.json"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "swerefactor.pytest_module",
         "-q", "-p", "no:cacheprovider", "test_mod.py"],
        cwd=work, capture_output=True, text=True,
        env={"PYTHONPATH": str(INFRA), "SRB_RESULT": str(out),
             "PATH": "/usr/bin:/bin"},
    )
    assert out.exists(), f"plugin wrote no payload:\n{proc.stdout}\n{proc.stderr}"
    return {c["id"].split("::")[-1]: c for c in json.loads(out.read_text())["checks"]}


def test_every_test_becomes_a_check(payload):
    assert len(payload) == 6


def test_pass_and_fail_keep_their_verdicts(payload):
    assert payload["test_passes"]["verdict"] == "pass"
    assert payload["test_fails"]["verdict"] == "fail"
    # The author's sentence is the headline and pytest's rewrite sits under it.
    # This line asserted the reverse, because `_summary` walked the `E` block
    # backwards and the rewrite came out on top.  Both halves of the property
    # are still pinned -- the rewrite in the field it belongs to -- so the fix
    # did not retire the check that the rewrite survives somewhere.
    assert payload["test_fails"]["summary"] == "AssertionError: a real assertion"
    assert "assert 1 == 2" in payload["test_fails"]["detail"]


def test_a_licensed_skip_stays_neutral(payload):
    check = payload["test_licensed_skip"]
    assert check["verdict"] == "skip"
    assert check["metadata"]["permitted_skip"] is True


def test_an_unlicensed_skip_is_a_miss(payload):
    assert payload["test_unlicensed_skip"]["verdict"] == "fail"
    assert payload["test_unlicensed_skip_missing_artifact"]["verdict"] == "fail"


@pytest.mark.parametrize("name", ["test_unlicensed_skip",
                                  "test_unlicensed_skip_missing_artifact",
                                  "test_unlicensed_skip_bare"])
def test_an_unlicensed_skip_explains_itself(payload, name):
    """The regression this file exists for.

    `_summary` returns the truthy word "skipped" for a skip, so an `or` fallback
    could never supply the explanation, and the detail was guarded on a local
    variable the skip branch does not update.  Both together sent every
    unlicensed skip to the report as the bare word "skipped".
    """
    check = payload[name]
    assert check["summary"] != "skipped"
    assert "does not permit" in check["summary"]
    assert check.get("detail"), "a miss with no detail explains nothing"
    assert "did not run" in check["detail"]


@pytest.mark.parametrize("name,reason", [
    ("test_unlicensed_skip", "the jar was never produced"),
    ("test_unlicensed_skip_missing_artifact", "no module-info.class in the jar"),
    ("test_licensed_skip", "the optional macro was not implemented"),
])
def test_the_suites_own_reason_survives(payload, name, reason):
    assert reason in payload[name]["summary"]


def test_pytests_filler_reason_is_not_repeated(payload):
    """A bare `@pytest.mark.skip` gets "unconditional skip", which says nothing."""
    assert "unconditional" not in payload["test_unlicensed_skip_bare"]["summary"]


def test_no_payload_carries_a_build_machine_path(payload):
    """A skip's longrepr is `(path, lineno, reason)` and that path is absolute."""
    for name, check in payload.items():
        for field in ("summary", "detail"):
            text = check.get(field) or ""
            assert "/tmp/" not in text and not text.startswith("/"), \
                f"{name}.{field} leaks a path: {text[:120]}"


def run_module(tmp_path: Path, body: str, *args: str) -> dict:
    """Run ``body`` as a pytest module under the plugin; return the JSON result."""
    (tmp_path / "test_sample.py").write_text(body, encoding="utf-8")
    result = tmp_path / "result.json"
    env = dict(os.environ)
    env["SRB_RESULT"] = str(result)
    env["PYTHONPATH"] = str(INFRA) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "swerefactor.pytest_module",
         "-q", "--no-header", str(tmp_path / "test_sample.py"), *args],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
    )
    assert result.exists(), (
        f"plugin wrote no result\n--- stdout\n{proc.stdout}\n--- stderr\n{proc.stderr}"
    )
    return json.loads(result.read_text(encoding="utf-8"))


def by_name(payload: dict) -> dict[str, dict]:
    return {c["id"].split("::", 1)[-1]: c for c in payload["checks"]}


# --------------------------------------------------------------------------
# _summary: the headline is the author's sentence, not pytest's restatement
# --------------------------------------------------------------------------

SUMMARY_CASES = """
import pytest


def test_assert_with_a_multiline_message():
    hits = {"pkg/a.go": ["dep"], "pkg/b.go": ["dep"]}
    assert not hits, (
        f"{len(hits)} delivered source file(s) still import dep:\\n"
        + "\\n".join(f"  {rel}" for rel in sorted(hits))
    )


def test_fail_with_a_multiline_message():
    pytest.fail("go.mod still requires dep (DIRECT): dep v1.8.1\\n"
                "files importing it: 13")


def test_assert_with_no_message():
    left, right = 2, 3
    assert left == right
"""


def test_summary_is_the_first_line_of_the_authors_message(tmp_path):
    """The regression this file exists for.

    Both failing shapes below put a self-contained sentence first and something
    useless last: pytest appends its own ``assert not {...}`` dict dump under
    the author's message, and a two-line ``pytest.fail`` ends on a clause that
    does not name its own subject.  Reading backwards picked the useless end of
    each.
    """
    checks = by_name(run_module(tmp_path, SUMMARY_CASES))

    multiline = checks["test_assert_with_a_multiline_message"]["summary"]
    assert multiline.startswith("AssertionError: 2 delivered source file(s) "
                                "still import dep")
    assert "assert not {" not in multiline

    failed = checks["test_fail_with_a_multiline_message"]["summary"]
    assert failed == "Failed: go.mod still requires dep (DIRECT): dep v1.8.1"
    assert "files importing it" not in failed


def test_summary_still_reports_a_bare_assert(tmp_path):
    """With no message there is nothing but pytest's rewrite, and it is enough.

    Guards the fix from being read as "prefer the author's message", which
    would leave this case with an empty headline.
    """
    summary = by_name(run_module(tmp_path, SUMMARY_CASES))[
        "test_assert_with_no_message"]["summary"]
    assert summary
    assert "2 == 3" in summary


def test_detail_keeps_what_the_summary_dropped(tmp_path):
    """The dict dump is still recoverable; only the headline changed."""
    check = by_name(run_module(tmp_path, SUMMARY_CASES))[
        "test_assert_with_a_multiline_message"]
    assert "pkg/a.go" in check["detail"]
    assert "pkg/b.go" in check["detail"]


def test_a_passing_check_has_no_summary(tmp_path):
    payload = run_module(tmp_path, "def test_ok():\n    assert True\n")
    check = payload["checks"][0]
    assert check["verdict"] == "pass"
    assert check["summary"] == ""
    assert "detail" not in check


# --------------------------------------------------------------------------
# the skip licence
# --------------------------------------------------------------------------

SKIP_CASES = """
import pytest


@pytest.mark.srb_skip_ok
def test_licensed_skip():
    pytest.skip("optional macro not implemented")


def test_unlicensed_skip():
    pytest.skip("the artifact was never produced")
"""


def test_an_unlicensed_skip_is_scored_as_a_miss(tmp_path):
    """The rule the whole denominator rests on.

    A submission can cause a skip by never producing the artifact under test.
    Left neutral, that would shrink the set of checks it is scored over, so the
    policy is enforced here rather than in the scorer.
    """
    checks = by_name(run_module(tmp_path, SKIP_CASES))

    unlicensed = checks["test_unlicensed_skip"]
    assert unlicensed["verdict"] == "fail"
    assert unlicensed["summary"]

    licensed = checks["test_licensed_skip"]
    assert licensed["verdict"] == "skip"
    assert licensed["metadata"]["permitted_skip"] is True


def test_an_unlicensed_skip_always_says_why_it_failed(tmp_path):
    """A rewritten verdict with an empty headline is an unexplained zero."""
    payload = run_module(tmp_path, "import pytest\n\n\n"
                                   "def test_bare_skip():\n"
                                   "    pytest.skip()\n")
    check = payload["checks"][0]
    assert check["verdict"] == "fail"
    assert check["summary"].strip()


# --------------------------------------------------------------------------
# weights, requirement, notes
# --------------------------------------------------------------------------

WEIGHT_CASES = """
import pytest


@pytest.mark.srb_weight(0.25)
def test_quarter():
    assert True


@pytest.mark.srb_weight(-4)
def test_negative_is_clamped():
    assert True


@pytest.mark.srb_weight("not a number")
def test_unparseable_falls_back():
    assert True


def test_plain():
    assert True


def test_note(request):
    request.node.user_properties.append(("srb_note", "decided at run time"))
    assert True
"""


def test_weights_come_off_the_marker(tmp_path):
    checks = by_name(run_module(tmp_path, WEIGHT_CASES))
    assert checks["test_quarter"]["weight"] == 0.25
    assert checks["test_plain"]["weight"] == 1.0


def test_a_negative_weight_cannot_subtract(tmp_path):
    """``max(0.0, ...)``: a module's total is a sum, so a negative would pay a
    submission for failing something else."""
    assert by_name(run_module(tmp_path, WEIGHT_CASES))[
        "test_negative_is_clamped"]["weight"] == 0.0


def test_an_unparseable_weight_does_not_silently_zero_the_check(tmp_path):
    """A typo in a marker should leave the check scoring what it always did,
    not quietly remove it from the module."""
    assert by_name(run_module(tmp_path, WEIGHT_CASES))[
        "test_unparseable_falls_back"]["weight"] == 1.0


def test_no_check_carries_required(tmp_path):
    """No check carries `required`, and this is what keeps it that way.

    It would mean "one failure here zeroes the module", a distinction the stage does
    not draw: stage 2 is all-or-nothing over every weighted module, so one failed
    check anywhere already costs everything a module-wide flag could.
    The marker is unregistered, so under `--strict-markers` a task applying it fails
    loudly rather than carrying a flag no scorer reads -- but the plugin could still
    invent the field from a stray keyword, and a `required` in `behavioural.json`
    reads as a live rule to whoever finds it next.
    """
    for check in by_name(run_module(tmp_path, WEIGHT_CASES)).values():
        assert "required" not in check, check["id"]


def test_a_run_time_note_reaches_the_report(tmp_path):
    """What makes a ``weight: 0.0`` entry self-explaining, so the zero is not
    indistinguishable from a mistake."""
    assert by_name(run_module(tmp_path, WEIGHT_CASES))[
        "test_note"]["metadata"]["note"] == "decided at run time"


# --------------------------------------------------------------------------
# errors, and which report wins
# --------------------------------------------------------------------------

PHASE_CASES = """
import pytest


@pytest.fixture
def broken():
    raise RuntimeError("fixture could not build the tree")


def test_setup_error(broken):
    assert True


@pytest.fixture
def noisy_teardown():
    yield
    raise RuntimeError("teardown failed after the assertion passed")


def test_teardown_error(noisy_teardown):
    assert True
"""


def test_a_setup_failure_is_an_error_not_a_fail(tmp_path):
    """It never reached the assertion, so it is not evidence about the tree."""
    check = by_name(run_module(tmp_path, PHASE_CASES))["test_setup_error"]
    assert check["verdict"] == "error"
    assert "fixture could not build the tree" in check["detail"]


def test_a_teardown_failure_does_not_overwrite_a_passing_call(tmp_path):
    """The first decisive verdict wins, and ``pass`` is not decisive against a
    later error -- but an error after a pass still has to be visible."""
    check = by_name(run_module(tmp_path, PHASE_CASES))["test_teardown_error"]
    assert check["verdict"] == "error"


def test_the_report_counts_every_check_once(tmp_path):
    payload = run_module(tmp_path, PHASE_CASES)
    ids = [c["id"] for c in payload["checks"]]
    assert len(ids) == len(set(ids)) == 2
    assert payload["metadata"] == {"runner": "pytest", "tests": 2}


def test_the_result_is_written_atomically(tmp_path):
    """``os.replace`` over a tempfile in the same directory: a reader either
    sees the previous file or the complete new one."""
    payload = run_module(tmp_path, "def test_ok():\n    assert True\n")
    assert payload["checks"]
    leftovers = list(tmp_path.glob(".tmp-*"))
    assert not leftovers, f"temp files left behind: {leftovers}"


# --------------------------------------------------------------------------- #
# the same plugin from lang04's side: one suite per defect, read
# through a subprocess, with the reason a skip carries as the subject
# --------------------------------------------------------------------------- #

# The pytest plugin that turns test outcomes into Checks.
#
# Written after a lang04 stage-1 run put two checks in front of a reviewer reading
# `verdict=fail`, `summary="skipped"` and an empty detail, with the author's own
# explanation -- which named the check reporting the same fact properly -- dropped
# on the floor.  Two independent defects produced that, and neither was covered:
# this module had no tests at all.
#
# The plugin is exercised as a subprocess rather than through `pytester`, because
# the thing under test is what a module's `run.sh` actually does: invoke pytest with
# `-p swerefactor.pytest_module` and read the JSON it leaves at `$SRB_RESULT`.


def run_suite(tmp_path, body: str) -> dict[str, dict]:
    """Run `body` as a test file under the plugin; return records by test name."""
    (tmp_path / "test_subject.py").write_text("import pytest\n\n" + body)
    out = tmp_path / "result.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(INFRA) + os.pathsep + env.get("PYTHONPATH", "")
    env["SRB_RESULT"] = str(out)
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "swerefactor.pytest_module", "test_subject.py"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
    )
    assert out.is_file(), "the plugin wrote no result file"
    return {c["id"].split("::")[-1]: c
            for c in json.loads(out.read_text())["checks"]}


def test_an_unlicensed_skip_keeps_the_reason_it_skipped_for(tmp_path):
    """The measured defect, both halves of it.

    lang04's `test_manifests_declare_no_dependencies` skips with "no Cargo.toml;
    closure/test_cargo_workspace_present reports it".  The harness recorded that
    as `fail` / "skipped" / no detail, so the reviewer could not tell an
    inapplicable check from a broken one, and could not see that another check
    already reported the same fact -- which is what stops them counting it twice.

    Two mechanisms lost it.  `record["summary"] or "..."` could never fire,
    because _summary() returns the truthy "skipped" for a skip; and the detail
    branch tests the *local* `verdict`, still "skip" after the record's verdict
    was rewritten, so nothing was attached.
    """
    checks = run_suite(tmp_path, '''
def test_no_manifest():
    pytest.skip("no Cargo.toml; closure/test_cargo_workspace_present reports it")
''')
    rec = checks["test_no_manifest"]
    assert rec["verdict"] == "fail"
    assert "no Cargo.toml" in rec["summary"]
    assert "closure/test_cargo_workspace_present" in rec["summary"]
    assert "does not permit" in rec["summary"]
    # And the detail says why the harness rewrote it, not just what happened.
    assert "srb_skip_ok" in rec["detail"]
    assert "no Cargo.toml" in rec["detail"]


def test_a_licensed_skip_stays_a_skip(tmp_path):
    """The control: with the marker, a skip is recorded as a skip and keeps its reason.

    `pooled` charges a skip, so the marker decides only how the check is
    *recorded*: a licensed skip keeps the suite's own wording and
    `permitted_skip`, an unlicensed one is rewritten to
    `fail` with the harness's explanation.  Both cost the module the same credit,
    and the difference is what a reader is told about why.
    """
    checks = run_suite(tmp_path, '''
@pytest.mark.srb_skip_ok
def test_stands_down():
    pytest.skip("State A is not mounted")
''')
    rec = checks["test_stands_down"]
    assert rec["verdict"] == "skip"
    assert rec["metadata"]["permitted_skip"] is True


def test_an_unexplained_skip_is_reported_as_unexplained(tmp_path):
    """A bare `pytest.skip()` renders as the word "Skipped" and nothing else.

    Passing that through as the reason produced "skipped (Skipped), which the
    contract does not permit", which reads as though a reason were given.  An
    author who skipped without saying why should be visible as exactly that.
    """
    checks = run_suite(tmp_path, '''
def test_bare():
    pytest.skip()
''')
    rec = checks["test_bare"]
    assert rec["verdict"] == "fail"
    assert rec["summary"] == "skipped, which the contract does not permit"
    assert "Skipped" not in rec["summary"]


def test_a_reclassified_skip_carries_no_suite_path(tmp_path):
    """A skip's longrepr is a (path, lineno, reason) triple.

    Attaching it whole would put the suite's location inside the verifier image
    into a graded payload and pin the reference's line numbers -- the same reason
    a stack trace does not belong in one.  Only the reason travels.
    """
    checks = run_suite(tmp_path, '''
def test_located():
    pytest.skip("a reason worth keeping")
''')
    rec = checks["test_located"]
    assert rec["verdict"] == "fail"
    assert "a reason worth keeping" in rec["summary"]
    blob = rec["summary"] + rec["detail"]
    assert "test_subject.py" not in blob
    assert str(tmp_path) not in blob


def test_a_genuine_failure_still_carries_its_traceback(tmp_path):
    """The negative control for the detail branch.

    That branch deliberately tests the local `verdict` rather than the record's,
    so a reclassified skip does not reach it.  A test that actually failed must
    still get its longrepr, or the fix above would have traded one silent check
    for another.
    """
    checks = run_suite(tmp_path, '''
def test_really_fails():
    assert 1 == 2, "the assertion message a reviewer needs"
''')
    rec = checks["test_really_fails"]
    assert rec["verdict"] == "fail"
    assert "the assertion message a reviewer needs" in rec["detail"]


# --------------------------------------------------------------------------
# the two reductions, called directly on blocks of the shape proved above
# --------------------------------------------------------------------------


class Report:
    """Only the attribute `_summary` reads."""

    def __init__(self, longrepr: str) -> None:
        self.longrepr = longrepr


# A header mismatch, in the shape compare.py emits: sentence, then the dump.
HEADER_BLOCK = """\
test_headers.py:31: in test_headers
    compare_headers(got, want, case, name)
/tests/behavioural/lib/compare.py:118: in compare_headers
    assert False, f"{case}::{name}: header mismatch"
E   AssertionError: index::ix-favicon-head: header mismatch
E   missing: {
E     'content-type': 'image/svg+xml',
E   }
E   unexpected: {
E     'content-type': 'text/html; charset=utf-8',
E   }
"""


def test_the_summary_is_the_authors_sentence_not_the_dumps_last_line():
    assert _summary(Report(HEADER_BLOCK), "fail") == (
        "AssertionError: index::ix-favicon-head: header mismatch"
    )


def test_a_trailing_bracket_is_never_the_summary():
    # The exact defect: 20 of one run's 47 failures had `]` as their headline.
    assert _summary(Report(HEADER_BLOCK), "fail").strip() not in ("]", "}")


def test_a_sentence_introducing_a_dump_loses_its_opening_bracket():
    block = "E   AssertionError: body differs: {\nE     'a': 1,\nE   }\n"
    assert _summary(Report(block), "fail") == "AssertionError: body differs"


def test_a_block_with_no_e_lines_falls_back_to_its_last_line():
    # An error rather than an assertion -- a collection failure, say -- has no E
    # block at all, and the bottom line is the one that names the exception.
    block = "during collection\nImportError: no module named 'axum'\n"
    assert _summary(Report(block), "error") == "ImportError: no module named 'axum'"


def test_a_pass_has_no_summary():
    assert _summary(Report(HEADER_BLOCK), "pass") == ""


def test_a_skip_with_an_empty_block_still_says_something():
    """Asserted on the property, not on the word, because the word changed.

    This arrived pinning ``== "skipped"``, which was the value at the time and is
    the value `test_an_unlicensed_skip_explains_itself` above exists to forbid.
    Both are the same finding seen from two sides -- a skip whose summary is the
    bare truthy word "skipped" tells a reader nothing and defeats an `or`
    fallback -- so the assertion is on what the test's own name promises.
    """
    out = _summary(Report(""), "skip")
    assert out and out != "skipped"
    assert "no reason recorded" in out


def test_the_summary_is_bounded():
    assert len(_summary(Report("E   " + "x" * 900 + "\n"), "fail")) <= 300


def test_a_short_detail_is_untouched():
    text = "E   AssertionError: nope\n"
    assert _detail(text) == text


def test_a_long_detail_keeps_both_ends_and_stays_within_budget():
    # The property the old `text[-1800:]` broke: the frames and the author's
    # sentence are at the top, the divergence is at the bottom, and a reader
    # needs both.
    text = "HEAD-MARKER\n" + "filler line\n" * 900 + "TAIL-MARKER"
    out = _detail(text)
    assert len(out) <= DETAIL_LIMIT
    assert out.startswith("HEAD-MARKER")
    assert out.endswith("TAIL-MARKER")


def test_the_elided_count_is_the_count_actually_dropped():
    # A first draft of `_detail` overstated by 31 characters, which is worse than
    # printing no number, because a reader cannot tell an approximation from a
    # fact.
    text = "".join(chr(65 + (i % 26)) for i in range(5000))
    out = _detail(text)
    marker = re.search(r"\n\[srb: (\d+) characters elided[^\]]*\]\n", out)
    assert marker is not None
    kept = len(out) - len(marker.group(0))
    assert int(marker.group(1)) + kept == len(text)


def test_the_gap_is_marked_so_it_cannot_be_read_as_the_block():
    assert "[srb:" in _detail("x" * (DETAIL_LIMIT + 1))


def test_a_detail_at_exactly_the_budget_is_not_marked():
    out = _detail("x" * DETAIL_LIMIT)
    assert out == "x" * DETAIL_LIMIT and "[srb:" not in out


def test_the_first_six_lines_carry_the_failure():
    """`report.py:40` renders `detail`'s first 6 lines, and only those."""
    text = HEADER_BLOCK + "filler line\n" * 900
    head = _detail(text).splitlines()[:6]
    assert any("AssertionError" in line for line in head)


# --------------------------------------------------------------------------
# the recorder's two fields, driven through a fake report
# --------------------------------------------------------------------------
# A third branch's tests, kept whole: they reach the recorder directly rather
# than through a subprocess, which is what lets them assert on a transcribed
# real-run block -- fw06's registration report -- without generating a module
# that reproduces it.  Three assertions are adapted where they pinned a value
# this tree resolved the other way; each says so at the site.


class FakeReport:
    """Enough of a pytest report for the recorder: it reads six attributes."""

    def __init__(self, longrepr: str = "", *, nodeid: str = "t.py::test_x",
                 when: str = "call", outcome: str = "failed",
                 keywords=(), duration: float = 0.0) -> None:
        self.nodeid = nodeid
        self.when = when
        self.outcome = outcome
        self.longrepr = longrepr
        self.keywords = {k: 1 for k in keywords}
        self.user_properties = []
        self.duration = duration


# A `pytest.fail` with a multi-line message: the sentence is the FIRST E line and
# every line after it is a supporting row.  Transcribed from fw06's registration
# report, which is where the bug was measured.
FAIL_REPORT = """\
modules/dispatch/test_dispatch.py:116: in test_report_the_registration_table
    pytest.fail(
E   Failed: REPORT (not a defect): 4 registration(s) over 1 distinct pattern literal(s)
E     with a method: []
E     without:       ['/']
E     pkg/server.go:99: Handler: r,
E     pkg/server.go:168: .Handle /
"""

# An assert that pytest rewrote: the message is again first, and the last E line
# is the raw expression, which says nothing a reader wants.
ASSERT_REPORT = """\
modules/dispatch/test_dispatch.py:368: in test_the_container_contract_is_unchanged
    assert not diffs, "the container contract changed:\\n" + ...
E   AssertionError: the container contract changed:
E     build: 'RUN go build -o /go/bin/app' -> 'RUN go build -o /go/bin/srv ./cmd'
E   assert not ['build: ...']
"""

# An exception raised six frames deep: here the message is at the BOTTOM, after
# every frame it took to get there.
ERROR_REPORT = """\
modules/dispatch/test_dispatch.py:80: in test_report_the_registration_table
    for path, rel in _sources(repo)
modules/dispatch/test_dispatch.py:48: in _sources
    return list(srbscan.go_sources(tree))
lib/srbscan.py:112: in go_sources
    import tomllib
E   ModuleNotFoundError: No module named 'srbscan'
"""


# --------------------------------------------------------------------------- #
# summary: the sentence, not the last row
# --------------------------------------------------------------------------- #


def test_summary_is_the_message_not_the_final_row():
    """Measured: a report of "45 literals in 5 files" was headlined by one file.

    On a submission that had vendored the retired router, the check reporting 45
    request-path literals across five files announced itself to the reviewer as
    "internal/dispatch/route.go: 1 occurrence(s) of ['/']" -- the blandest row it
    had -- because the summary was read from the last E line rather than the
    first.  The headline field is the one thing a reviewer reads for every
    finding; spending it on a table's last row spends it on nothing.
    """
    assert _summary(FakeReport(FAIL_REPORT), "fail") == (
        "Failed: REPORT (not a defect): 4 registration(s) over 1 distinct "
        "pattern literal(s)"
    )


def test_summary_of_a_rewritten_assert_is_the_message_not_the_expression():
    got = _summary(FakeReport(ASSERT_REPORT), "fail")
    assert got.startswith("AssertionError: the container contract changed:")
    assert "assert not" not in got


def test_summary_of_an_exception_is_the_exception():
    """The single-E-line shape: first and last agree, so this must not regress."""
    assert _summary(FakeReport(ERROR_REPORT), "error") == (
        "ModuleNotFoundError: No module named 'srbscan'"
    )


def test_summary_of_a_pass_is_empty_and_of_a_silent_skip_is_not():
    assert _summary(FakeReport(outcome="passed"), "pass") == ""
    # Arrived pinning `== "skipped"`; asserted on the property instead, because
    # that exact value is what `test_an_unlicensed_skip_explains_itself` above
    # exists to forbid -- a skip whose headline is the bare truthy word tells a
    # reader nothing and defeats an `or` fallback.  Three branches found that.
    silent = _summary(FakeReport(""), "skip")
    assert silent and silent != "skipped"
    # No E line and no verdict word left: fall back to something, never crash.
    assert _summary(FakeReport("collection error"), "error") == "collection error"


def test_summary_is_capped():
    assert len(_summary(FakeReport("E   " + "x" * 900), "fail")) == 300


# --------------------------------------------------------------------------- #
# detail: both ends, because the two shapes carry their message at opposite ones
# --------------------------------------------------------------------------- #


def test_a_short_report_is_kept_whole():
    assert _detail(FAIL_REPORT) == FAIL_REPORT


def test_an_over_long_failure_keeps_its_headline():
    """An over-long failure is cut at the tail, because the headline is at the head.

    A budget applied to the tail begins the reviewer's copy mid-sentence, and the
    words that go first are the ones at the front: "REPORT (not a defect)" is what
    keeps a report from being read as an accusation, and it has to reach every
    consumer's window rather than being the first thing spent.
    """
    long = FAIL_REPORT + "".join(f"E     pkg/server.go:{i}: .Handle /x{i}\n"
                                 for i in range(400))
    assert len(long) > DETAIL_LIMIT
    got = _detail(long)
    assert got.startswith("modules/dispatch/test_dispatch.py:116:")
    assert "REPORT (not a defect)" in got
    # And the front is intact far enough for every consumer's window.
    assert "REPORT (not a defect)" in got[:240]
    assert "elided" in got


def test_an_over_long_traceback_keeps_its_exception():
    """The regression a head-only budget would cause, asserted directly."""
    deep = ("modules/dispatch/test_dispatch.py:80: in test_x\n"
            + "".join(f"lib/srbscan.py:{i}: in helper{i}\n    call{i}()\n"
                      for i in range(200))
            + "E   ModuleNotFoundError: No module named 'srbscan'\n")
    assert len(deep) > DETAIL_LIMIT
    got = _detail(deep)
    assert "ModuleNotFoundError: No module named 'srbscan'" in got
    assert got.startswith("modules/dispatch/test_dispatch.py:80:")
    # A reader who only gets the summary still learns it, too.
    assert _summary(FakeReport(deep), "error").startswith("ModuleNotFoundError")


def test_the_detail_budget_is_respected():
    got = _detail("y" * 9000)
    assert len(got) <= DETAIL_LIMIT
    assert got.startswith("y" * DETAIL_HEAD)
    assert got.endswith("y")


def test_the_elided_count_is_the_number_actually_dropped():
    """The marker's number, checked rather than assumed present.

    ``"elided" in got`` passes on a wrong number as easily as a right one, and the
    number is easy to get wrong: the marker is charged to the budget, so it displaces
    content it is not counting, and a 4800-character report claiming 3000 characters
    elided where 3038 is true is off by exactly the marker's own length -- in the
    direction that flatters the truncation.

    Asserted by reconstructing the drop from the output's three parts, so the test
    cannot be satisfied by the same arithmetic that would produce the bug.
    """
    for size in (1801, 1839, 2000, 4800, 9000, 100_000):
        text = "H" * size
        got = _detail(text)
        assert len(got) <= DETAIL_LIMIT, size

        marker = re.search(r"\n\[srb: (\d+) characters elided[^\]]*\]\n", got)
        assert marker, (size, got[:200])
        claimed = int(marker.group(1))
        kept = len(got) - len(marker.group(0))
        assert claimed == len(text) - kept, (size, claimed, len(text) - kept)

    # And the head is never sacrificed to make room for the marker while the tail
    # survives: at the budget's edge the tail is what goes.
    edge = _detail("H" * 1801)
    assert edge.startswith("H" * 200)


# --------------------------------------------------------------------------- #
# end to end through the recorder, since that is what writes the file
# --------------------------------------------------------------------------- #


def test_a_failing_record_carries_both_fields():
    rec = _Recorder("")
    rec.observe(FakeReport(FAIL_REPORT))
    record = rec.records["t.py::test_x"]
    assert record["verdict"] == "fail"
    assert "required" not in record, "the flag was removed with the rule"
    assert record["summary"].startswith("Failed: REPORT (not a defect):")
    # "Both fields", and the detail is the one that carries what the summary
    # dropped.  For a fail the merged recorder strips the leading
    # `modules/dispatch/test_dispatch.py:116:`, and `authored` records why: the
    # location line pins the verifier's own line numbers into a payload a model
    # reads, so reformatting a test file would change the graded input.  An `error`
    # keeps its frames, and `test_an_over_long_traceback_keeps_its_exception` above
    # pins that.
    detail = record["detail"]
    assert "modules/dispatch/test_dispatch.py:116:" not in detail
    assert "without:       ['/']" in detail, "the supporting rows are the detail"
    assert len(detail) > len(record["summary"])


def test_a_passing_record_carries_no_detail():
    rec = _Recorder("")
    rec.observe(FakeReport("", outcome="passed"))
    record = rec.records["t.py::test_x"]
    assert record["verdict"] == "pass"
    assert "detail" not in record
    assert record["summary"] == ""


# --------------------------------------------------------------------------- #
# end-to-end cases, on the contract this tree settled
# --------------------------------------------------------------------------- #
#
# Driven through real pytest, for the reason the sections above are: the failure mode
# being guarded is a wrong belief about what pytest writes, and a stub would encode
# the same belief.  Two decisions the cases below depend on:
#
#   `summary` keeps the exception type.  Stripping a leading ``AssertionError:``
#   because every failed assert is one, while keeping ``TypeError: yargs.config is
#   not a function``, is a rule with no edge -- and a reviewer reading a digest of
#   headlines has nothing else to tell an assertion apart from a crash.  Eight
#   assertions in the sections above pin it, and `_summary`'s docstring records the
#   reason.
#
#   `detail` keeps its first line.  Dropping a repeat of the summary, or an ``assert``
#   restatement, happens one layer down at `scan._detail_beyond_summary` and
#   deliberately not here: this artifact's numbers are what stage 2's identity run is
#   pinned to, the report prints summary and detail in different places and wants
#   both, and dropping line one at the producer would empty the detail of a bare
#   ``assert 2 == 3`` whose whole E block is that one line.


def run_one(tmp_path: Path, body: str) -> dict:
    """One check's record, end to end.  `import pytest` is prepended for the body.

    Distinct from `run_module` only in that it prepends the import and unwraps the
    single check, which is what the cases below want; sharing the runner keeps
    there being one description of how a module is actually invoked.
    """
    payload = run_module(tmp_path, "import pytest\n\n" + body)
    checks = payload["checks"]
    assert len(checks) == 1, f"expected one check, got {[c['id'] for c in checks]}"
    return checks[0]


def test_the_headline_is_the_authors_sentence_on_the_measured_fw07_check(tmp_path):
    """The fw07 measurement, as its own scenario: 21 headlines were the restatement.

    A scan check asserts on a dict of hits and explains in its message what a hit
    means.  Reading the E block backwards returned ``assert not {...}`` and threw
    the sentence away, so the reviewer's headline for this finding was a dict repr
    keyed on a path -- and the headline is most of what a model sees before it
    decides whether a finding is a defect.

    The sentence ends in a colon and the citation follows on the next line, which is
    the idiom `_headline` continues onto line two rather than stopping at: 53 sites
    across five tasks write it, and stopping at the colon names a category while
    withholding every particular.  So the citation is expected *in the summary*
    here, and that is why the consumer may drop it from the detail below.
    """
    check = run_one(tmp_path, '''
def test_raw_path_is_not_read():
    # `rel:lineno: text`, which is what srbscan.cite() actually emits.
    hits = {"navigation/src/main/java/NavigateResource.java":
            ["navigation/src/main/java/NavigateResource.java:254: "
             "String url = httpServletRequest.getRequestURI();"]}
    assert not hits, (
        f"'getRequestURI(' appears in {len(hits)} delivered source file(s); an "
        f"annotated handler does not need the raw path, a dispatcher does:\\n"
        + "\\n".join(line for rel in sorted(hits) for line in hits[rel]))
''')
    assert check["verdict"] == "fail"
    summary = check["summary"]
    assert summary.startswith(
        "AssertionError: 'getRequestURI(' appears in 1 delivered source"), \
        f"headline is not the author's sentence: {summary!r}"
    assert "an annotated handler does not need the raw path" in summary
    assert not summary.startswith("AssertionError: assert not {"), \
        "the summary is pytest's restatement of the assert expression"
    assert "NavigateResource.java:254" in summary, \
        "the headline stopped at the colon and withheld the citation"
    # The detail opens with that same sentence, and the consumer is what removes it.
    assert check["detail"].splitlines()[1].startswith("navigation/")


def test_the_exception_type_is_kept_for_an_assert_and_for_a_crash(tmp_path):
    """Both halves of the rule that has no edge, in one test because that is the point.

    The branch this case comes from stripped ``AssertionError:`` and kept
    ``ZeroDivisionError:``.  The two records below are what that rule produces: a
    reviewer holding both cannot tell which check *failed* and which one *broke*,
    and that is the first thing they need, because a broken check is a bug in the
    verifier and a failed one is a finding about the submission.
    """
    assert run_one(tmp_path, '''
def test_plain_assert():
    assert 1 == 2, "the two trees disagree on the connector's port"
''')["summary"] == \
        "AssertionError: the two trees disagree on the connector's port"

    broke = run_one(tmp_path, '''
def test_check_itself_is_broken():
    1 / 0
''')
    assert broke["summary"] == "ZeroDivisionError: division by zero"


def test_a_deliberate_report_keeps_its_census_line_behind_the_failed_prefix(tmp_path):
    """``pytest.fail`` in a REPORT check: the prefix stays, the census survives it.

    The scan modules fail on purpose to surface a census, so these are the headlines
    a reviewer reads most often.  ``Failed:`` is eight characters of the 300 and it
    is kept for the same reason as ``AssertionError:`` -- and what matters here is
    the other end of the assertion: the words ``REPORT (not a defect)``, which are
    what stop the line being read as an accusation, must be inside the cap.
    """
    check = run_one(tmp_path, '''
def test_report_handlers():
    pytest.fail("REPORT (not a defect): 12 of 14 handler-declaring class(es) "
                "carry a Spring annotation")
''')
    assert check["summary"].startswith(
        "Failed: REPORT (not a defect): 12 of 14")
    assert check["summary"].endswith("carry a Spring annotation")


def test_a_multiline_message_carries_its_first_offender_into_the_headline(tmp_path):
    """A sentence, then a list: the headline takes the sentence and one item.

    The branch this comes from expected the headline to stop at the sentence and the
    list to be the whole detail.  This tree continues past a trailing colon, so the
    headline names the category *and* one particular -- which is the difference
    between "3 resource class(es) still extend the Dropwizard base:" and a headline
    a reader can act on without opening the detail.  The rest of the list survives
    below it, in order.
    """
    check = run_one(tmp_path, r'''
def test_many_offenders():
    assert False, "3 resource class(es) still extend the Dropwizard base:\n  a\n  b\n  c"
''')
    assert check["summary"] == \
        "AssertionError: 3 resource class(es) still extend the Dropwizard base: a"
    assert check["detail"].splitlines()[:4] == [
        "AssertionError: 3 resource class(es) still extend the Dropwizard base:",
        "a", "b", "c"]


def test_a_long_message_is_capped_at_300_characters_end_to_end(tmp_path):
    """The cap through a real pytest run, not through a fabricated block.

    The direct-call sections pin `_headline`'s boundary with short strings.  This
    asserts the number a reviewer's prompt is actually built from, which is the one
    that has to hold: pytest wraps and re-indents a long message on its way into the
    E block, so a cap applied before that wrapping is not the cap that ships.
    """
    assert len(run_one(tmp_path, '''
def test_long_message():
    assert False, "x" * 900
''')["summary"]) == 300


def test_an_exception_raised_in_the_body_is_the_whole_headline(tmp_path):
    """A check that broke has no author sentence, so the E block's one line is it.

    The fixture sections above raise from a fixture, which is a setup error and a
    different verdict.  Raised in the body it is a `fail`, and dropping the frames
    must not leave it with nothing -- that would make a verifier bug look like a
    silent pass.
    """
    check = run_one(tmp_path, '''
def test_broken():
    raise RuntimeError("the scan could not read the tree")
''')
    assert check["verdict"] == "fail"
    assert check["summary"] == "RuntimeError: the scan could not read the tree"


def test_the_detail_carries_no_frame_and_the_consumer_drops_the_repeat(tmp_path):
    """The carried case, with its three assertions at the two layers that hold them.

    Its measurement: on a real fw07 prompt a check that named two offending files
    spent its 240 characters saying its own headline again and citing a line in the
    verifier, so the files -- which appear nowhere else, since `evidence` is empty in
    this suite -- were pushed out.  Three things were charged for that, and they do
    not all belong in the same place.

    The frame is dropped here, at the producer, and for a second reason besides
    budget: it pins the verifier's own line numbers and prints the build machine's
    paths into a graded payload.

    The repeated headline is dropped at the consumer.  `_detail_beyond_summary` is
    what `scan.digest` calls, and it strips the prefix *before* the 240-character cut
    -- which is the whole fix, since comparing two already-truncated strings can only
    catch a detail that is nothing but its summary.  Asserting it there rather than
    here is not a weaker test of the same thing: it is a test of the string the
    reviewer is handed, and it leaves this file's recorded numbers where stage 2's
    identity run expects them.

    pytest's ``assert not [...]`` restatement stays in the artifact deliberately, so
    a failure whose message says only that two things differ still has the line that
    names the operands.  It is last, so it is what the consumer's budget gives up
    first rather than what the offending paths give way to.
    """
    check = run_one(tmp_path, '''
def test_two_offenders():
    hits = ["web-bundle/src/main/java/A.java:12", "web/src/main/java/B.java:34"]
    assert not hits, "2 file(s) still do it:\\n" + "\\n".join(hits)
''')
    detail = check["detail"]
    assert "test_sample.py" not in detail, "detail carries a frame"
    assert not detail.startswith("_"), "detail carries pytest's frame separator"
    assert detail.splitlines()[1:3] == ["web-bundle/src/main/java/A.java:12",
                                        "web/src/main/java/B.java:34"]
    assert detail.splitlines()[-1].startswith("assert not ["), \
        "the operand restatement is kept, and kept last"

    # And at the consumer, the repeat is gone and both paths survive the cut.
    beyond = _detail_beyond_summary(detail, check["summary"])
    assert not beyond.startswith("AssertionError:"), \
        "the reviewer is handed the headline twice"
    assert "web/src/main/java/B.java:34" in beyond, \
        "the second offender was pushed out by the repeat"
