"""The toolbox: path containment, truncation, and the shape of tool output.

The containment tests matter most.  The workspace is code written by an agent that
wants a good score, and it is read by a process that can see the grader's own
files -- so "this path stays inside the tree" has to be true of where a path
lands, not of how it is spelled.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor.tools import MAX_READ_BYTES, ToolError, toolbox_for


@pytest.fixture
def box(tmp_path: Path):
    original = tmp_path / "original"
    workspace = tmp_path / "workspace"
    (original / "src").mkdir(parents=True)
    (workspace / "src").mkdir(parents=True)
    (original / "src" / "a.c").write_text("int main(void) { return 0; }\n")
    (original / "README").write_text("original readme\n")
    (workspace / "src" / "a.rs").write_text(
        "fn main() {\n    println!(\"hello\");\n}\n")
    (workspace / "README").write_text("rewritten readme\n")
    (tmp_path / "secret.txt").write_text("the grading key\n")
    return toolbox_for(original, workspace), tmp_path


# --------------------------------------------------------------------------- #
# containment
# --------------------------------------------------------------------------- #


def test_traversal_out_of_a_root_is_refused(box):
    toolbox, tmp_path = box
    with pytest.raises(ToolError, match="escapes root|outside every readable"):
        toolbox.resolve("workspace:../secret.txt")


def test_an_absolute_path_outside_every_root_is_refused(box):
    toolbox, tmp_path = box
    with pytest.raises(ToolError, match="outside every readable root"):
        toolbox.resolve(str(tmp_path / "secret.txt"))


def test_a_symlink_pointing_out_of_the_tree_is_refused(box):
    """The submission can contain a symlink; the check is on where it lands."""
    toolbox, tmp_path = box
    link = toolbox.roots["workspace"].path / "escape"
    os.symlink(tmp_path / "secret.txt", link)
    with pytest.raises(ToolError, match="escapes root"):
        toolbox.resolve("workspace:escape")


def test_reading_through_a_symlink_reports_the_error_to_the_model(box):
    toolbox, tmp_path = box
    os.symlink(tmp_path / "secret.txt", toolbox.roots["workspace"].path / "esc")
    out = toolbox.dispatch("fetch_source", {"path": "workspace:esc"})
    assert out.startswith("ERROR")
    assert "the grading key" not in out


def test_a_bare_relative_path_means_the_workspace(box):
    toolbox, _ = box
    root, target = toolbox.resolve("src/a.rs")
    assert root.name == "workspace"
    assert target.name == "a.rs"


def test_an_absolute_path_inside_a_root_is_accepted(box):
    toolbox, _ = box
    inside = toolbox.roots["original"].path / "README"
    root, target = toolbox.resolve(str(inside))
    assert root.name == "original"
    assert target == inside


# --------------------------------------------------------------------------- #
# reading and searching
# --------------------------------------------------------------------------- #


def test_fetch_source_numbers_lines_and_reports_the_total(box):
    toolbox, _ = box
    out = toolbox.dispatch("fetch_source", {"path": "workspace:src/a.rs"})
    assert "lines 1-3 of 3" in out
    assert "     2\t    println!" in out


def test_a_line_range_is_honoured(box):
    toolbox, _ = box
    out = toolbox.dispatch("fetch_source", {"path": "workspace:src/a.rs",
                                        "start_line": 2, "end_line": 2})
    assert "lines 2-2 of 3" in out
    assert "fn main" not in out


def test_truncation_is_announced(box):
    toolbox, _ = box
    big = toolbox.roots["workspace"].path / "big.txt"
    big.write_text("x" * (MAX_READ_BYTES * 2))
    out = toolbox.dispatch("fetch_source", {"path": "workspace:big.txt"})
    assert "TRUNCATED" in out


def test_a_missing_file_is_an_error_the_model_can_act_on(box):
    toolbox, _ = box
    out = toolbox.dispatch("fetch_source", {"path": "workspace:nope.rs"})
    assert out.startswith("ERROR") and "does not exist" in out


def test_search_reports_path_and_line(box):
    toolbox, _ = box
    out = toolbox.dispatch("search", {"pattern": "println", "path": "workspace:"})
    assert "workspace:src/a.rs:2:" in out


def test_search_with_no_match_says_how_much_it_looked_at(box):
    toolbox, _ = box
    out = toolbox.dispatch("search", {"pattern": "import flask"})
    assert "no match" in out and "files scanned" in out


def test_a_bad_regex_comes_back_as_an_error(box):
    toolbox, _ = box
    out = toolbox.dispatch("search", {"pattern": "([unclosed"})
    assert out.startswith("ERROR") and "bad regex" in out


def test_list_dir_shows_sizes_and_marks_directories(box):
    toolbox, _ = box
    out = toolbox.dispatch("list_dir", {"path": "workspace:", "depth": 2})
    assert "src/" in out
    assert "README" in out


# --------------------------------------------------------------------------- #
# diff, which is the tool that answers the actual question
# --------------------------------------------------------------------------- #


def test_diff_across_roots(box):
    toolbox, _ = box
    out = toolbox.dispatch("diff", {"path": "README"})
    assert "-original readme" in out
    assert "+rewritten readme" in out


def test_diff_reports_a_file_that_was_deleted(box):
    toolbox, _ = box
    out = toolbox.dispatch("diff", {"path": "src/a.c"})
    assert "ABSENT" in out


def test_diff_reports_a_file_that_is_new(box):
    toolbox, _ = box
    out = toolbox.dispatch("diff", {"path": "src/a.rs"})
    assert "new in workspace" in out


def test_identical_files_say_so(box):
    toolbox, _ = box
    (toolbox.roots["original"].path / "same.txt").write_text("same\n")
    (toolbox.roots["workspace"].path / "same.txt").write_text("same\n")
    out = toolbox.dispatch("diff", {"path": "same.txt"})
    assert "byte-identical" in out


# --------------------------------------------------------------------------- #
# run, which is off unless the task asks for it
# --------------------------------------------------------------------------- #


def test_run_is_not_offered_when_no_commands_are_allowed(box):
    toolbox, _ = box
    assert not toolbox.handles("run")
    assert "run" not in {s.name for s in toolbox.specs()}


def test_a_command_outside_the_allowlist_is_refused(tmp_path):
    (tmp_path / "o").mkdir()
    (tmp_path / "w").mkdir()
    toolbox = toolbox_for(tmp_path / "o", tmp_path / "w",
                          allowed_commands=("echo",))
    out = toolbox.dispatch("run", {"argv": ["rm", "-rf", "/"]})
    assert out.startswith("ERROR") and "not permitted" in out


def test_an_allowed_command_runs_and_reports_its_exit_status(tmp_path):
    (tmp_path / "o").mkdir()
    (tmp_path / "w").mkdir()
    toolbox = toolbox_for(tmp_path / "o", tmp_path / "w",
                          allowed_commands=("echo",))
    out = toolbox.dispatch("run", {"argv": ["echo", "hello"]})
    assert "exit 0" in out and "hello" in out


def test_a_command_line_as_one_string_is_rejected_with_the_reason(tmp_path):
    (tmp_path / "o").mkdir()
    (tmp_path / "w").mkdir()
    toolbox = toolbox_for(tmp_path / "o", tmp_path / "w",
                          allowed_commands=("echo",))
    out = toolbox.dispatch("run", {"argv": "echo hello && cat /etc/passwd"})
    assert out.startswith("ERROR") and "no shell" in out


# --------------------------------------------------------------------------- #
# grounding
# --------------------------------------------------------------------------- #


def test_grounding_accepts_a_true_citation(box):
    toolbox, _ = box
    ok, note = toolbox.ground("workspace:src/a.rs", 2, "println!")
    assert ok and note == "verified"


def test_grounding_rejects_a_line_past_the_end(box):
    toolbox, _ = box
    ok, note = toolbox.ground("workspace:src/a.rs", 400, "")
    assert not ok and "has 3 lines" in note


def test_grounding_rejects_a_quote_that_is_not_there(box):
    toolbox, _ = box
    ok, note = toolbox.ground("workspace:src/a.rs", 1, "import flask")
    assert not ok


def test_grounding_allows_a_quote_a_few_lines_off(box):
    """Models miscount lines; the citation is still pointing at the right thing."""
    toolbox, _ = box
    ok, _ = toolbox.ground("workspace:src/a.rs", 3, "println!")
    assert ok


def test_grounding_rejects_a_path_that_does_not_exist(box):
    toolbox, _ = box
    ok, note = toolbox.ground("workspace:invented.rs", 1, "x")
    assert not ok and "does not exist" in note


# --------------------------------------------------------------------------- #
# a refusal the model can act on
# --------------------------------------------------------------------------- #

def test_claude_codes_read_arguments_are_mapped_rather_than_refused(box):
    """`file_path`/`limit`/`offset` must produce the file, not a complaint.

    Real cost of getting this wrong: in one graded stage 3, three adversaries
    sent those names -- Claude Code's Read arguments, substituted for this
    toolbox's own by the gateway -- and were told only "path must be a non-empty
    string".  One retried the identical call for eighty turns and ran zero tests.

    Naming the wrong word (the test below) is not enough on its own, because the
    model is not choosing the word: it is sent a tool definition carrying those
    names and cannot see the schema this toolbox declared.  So the arguments are
    mapped.  `limit` is a line count and `end_line` is a line number, which makes
    it a conversion: offset=2, limit=2 is lines 2 and 3, so end_line=3.
    """
    toolbox, _ = box
    out = toolbox.dispatch("fetch_source", {"file_path": "workspace:src/a.rs",
                                            "limit": 2, "offset": 2})
    assert not out.startswith("ERROR:"), out
    assert "println!" in out          # line 2, the slice's start
    assert "fn main" not in out       # line 1, outside it


def test_an_unknown_argument_name_is_named_along_with_the_real_ones(box):
    """A word that cannot be mapped still has to be identified, not just refused.

    The translation above covers the substitution seen in the wild.  This is the
    fallback for the next one: say which word was wrong and what the real ones
    are, so a retry can succeed.

    The example that motivated this -- `file_path`/`limit`/`offset` -- is no longer
    an unknown-argument case, because those are exactly the names the gateway
    substitutes and the toolbox now translates them.  A clearer refusal was
    measured not to help there: the model was complying with the schema it had been
    given, so it re-sent the same call up to fourteen times.  The diagnostic still
    has to exist for arguments that are simply wrong, so this uses such a name and
    checks that *every* unknown word is named, not just the first.
    """
    toolbox, _ = box
    out = toolbox.dispatch("fetch_source", {"filename": "workspace:src/a.rs",
                                            "lines": 200})
    assert out.startswith("ERROR:")
    for wrong in ("'filename'", "'lines'"):
        assert wrong in out, f"{wrong} not named in: {out}"
    # And the names that would have worked, so the retry can succeed.
    for right in ("path", "start_line", "end_line"):
        assert right in out
    assert "Nothing ran" in out


def test_a_correct_call_is_not_disturbed_by_the_check(box):
    toolbox, _ = box
    out = toolbox.dispatch("fetch_source", {"path": "workspace:src/a.rs"})
    assert not out.startswith("ERROR:")
    assert "println!" in out


def test_the_check_covers_every_tool_not_just_fetch_source(box):
    """A per-tool fix would leave the same trap set in search and list_dir."""
    toolbox, _ = box
    for name, bad in (("list_dir", {"directory": "workspace:"}),
                      ("search", {"regex": "hello"}),
                      ("diff", {"filename": "README"})):
        out = toolbox.dispatch(name, bad)
        key = next(iter(bad))
        assert out.startswith("ERROR:") and repr(key) in out, f"{name}: {out}"


# --------------------------------------------------------------------------- #
# argument names a gateway substitutes for ours
# --------------------------------------------------------------------------- #

def test_the_gateway_argument_names_are_translated_not_refused(box):
    """`read_file` reaches Claude models carrying Claude Code's Read schema.

    Measured on an Anthropic-compatible relay against claude-opus-5 and
    claude-sonnet-5: the model sends {file_path, limit, offset} because that is
    the schema it was handed, and it keeps sending them after being told they are
    wrong.  Three of six adversaries in one graded stage 3 spent their entire turn
    budget this way (90/90 and 21/21 calls refused) and still scored as survivals,
    because a turn-exhausted round is paid as SURVIVED.
    """
    toolbox, _ = box
    out = toolbox.dispatch("fetch_source", {"file_path": "workspace:src/a.rs",
                                         "limit": 200, "offset": 1})
    assert not out.startswith("ERROR:"), out
    assert "println!" in out
    assert toolbox.rewritten_args == 1


def test_offset_is_a_first_line_and_limit_is_a_count(box):
    """The mapping is arithmetic, not renaming, so the base matters.

    Probed rather than assumed, both models: "lines 100 through 139" arrives as
    offset=100, limit=40.  So the last line is offset + limit - 1, and reading it
    as offset + limit would return one line too many -- a wrong slice is worse
    than a refusal, because it is wrong evidence a reviewer will cite.
    """
    toolbox, _ = box
    got = toolbox._translate_gateway_args(
        "fetch_source", {"file_path": "workspace:big.txt", "offset": 100,
                      "limit": 40})
    assert got == {"path": "workspace:big.txt", "start_line": 100,
                   "end_line": 139}


def test_a_correctly_named_argument_is_never_clobbered(box):
    """A mixed call keeps what it got right."""
    toolbox, _ = box
    got = toolbox._translate_gateway_args(
        "fetch_source", {"path": "workspace:src/a.rs", "start_line": 2,
                      "limit": 1})
    assert got["path"] == "workspace:src/a.rs"
    assert got["start_line"] == 2 and got["end_line"] == 2


def test_an_unreadable_slice_drops_to_the_whole_file(box):
    """Junk in the slice must not be guessed at, and must not raise."""
    toolbox, _ = box
    out = toolbox.dispatch("fetch_source", {"file_path": "workspace:src/a.rs",
                                         "offset": "start", "limit": None})
    assert not out.startswith("ERROR:"), out
    assert "println!" in out


def test_untouched_calls_do_not_count_as_rewritten(box):
    """The counter is a signal about the gateway, so it must stay quiet."""
    toolbox, _ = box
    toolbox.dispatch("fetch_source", {"path": "workspace:src/a.rs"})
    toolbox.dispatch("list_dir", {"path": "workspace:"})
    assert toolbox.rewritten_args == 0


def test_other_tools_are_not_given_the_read_file_translation(box):
    """Only `read_file` is rewritten upstream; the rest keep their own names.

    Measured across all eight stage tool names: list_dir, search, diff, run,
    try_test, report and submit_review all arrive with the declared arguments.  So
    translating `offset` anywhere else would be inventing an argument the schema
    does not have.
    """
    toolbox, _ = box
    out = toolbox.dispatch("list_dir", {"file_path": "workspace:"})
    assert out.startswith("ERROR:") and "'file_path'" in out
    assert toolbox.rewritten_args == 0


def test_a_missing_required_argument_still_reports_the_old_way(box):
    """This check is about wrong names.  An omitted argument is a different
    mistake and must keep its own complaint rather than being called unknown."""
    toolbox, _ = box
    out = toolbox.dispatch("fetch_source", {})
    assert out.startswith("ERROR:") and "non-empty string" in out


# --------------------------------------------------------------------------- #
# names an agentic-CLI gateway claims for itself
# --------------------------------------------------------------------------- #

#: Tool names Claude Code defines natively.  A gateway fronting one of those CLIs
#: may recognise a name in this set and substitute its own schema for ours before
#: the model is ever shown a tool definition -- which is what happened to
#: ``read_file``, measured 2026-08-04 against the ``/cc`` gateway: the model
#: received ``{file_path, limit, offset}`` and could not have sent ``path``.
#: Only ``read_file`` was observed being rewritten; the rest are here because the
#: same substitution is available for any of them and the cost of finding out in
#: a graded run is a stage-3 round that reads nothing for its whole budget.
CLI_NATIVE_TOOL_NAMES = {
    "read_file", "read", "write", "edit", "bash", "glob", "grep",
    "web_fetch", "webfetch", "web_search", "websearch", "task",
    "notebook_edit", "notebookedit", "todo_write", "todowrite",
}


def test_no_declared_tool_takes_a_name_an_agentic_cli_defines(box):
    """Our schema has to be the one the model sees.

    A gateway that recognises the name substitutes its own arguments for ours,
    the model sends those arguments in good faith, and the toolbox refuses every
    call.  The arguments are also aliased (above), but that is the second line of
    defence and it only covers the substitutions already seen -- not taking the
    name at all is the first.
    """
    toolbox, _ = box
    for spec in toolbox.specs():
        assert spec.name.lower() not in CLI_NATIVE_TOOL_NAMES, (
            f"tool {spec.name!r} collides with an agentic CLI's own tool; a "
            f"gateway may rewrite its schema before the model sees it"
        )


def test_the_reader_is_still_reachable_under_the_name_it_declares(box):
    """Guards the rename itself: the spec and the method must not drift apart.

    Renaming the ToolSpec without renaming what dispatch calls would leave the
    tool advertised and dead, which is the failure this whole section exists to
    prevent, arriving by a different route.
    """
    toolbox, _ = box
    names = {s.name for s in toolbox.specs()}
    assert "fetch_source" in names
    for name in names:
        assert toolbox.handles(name), f"{name} is advertised but not dispatched"
        out = toolbox.dispatch(name, {})
        assert "no such tool" not in out, f"{name}: {out}"


# --------------------------------------------------------------------------- #
# a toolbox with no roots
# --------------------------------------------------------------------------- #

# The scope adjudicator is handed Toolbox(roots={}) deliberately: it rules on a
# candidate's text and must not be able to go and read either tree.  It is the one
# caller with no roots, so every path through the toolbox has to tolerate that.
# It did not: picking a default root ran next(iter({})) and raised StopIteration,
# which unwinds through a generator-driven caller as a silent stop rather than an
# error, and killed the stage from inside adjudication.


@pytest.fixture
def empty_box():
    from swerefactor.tools import Toolbox
    return Toolbox(roots={})


def test_a_toolbox_with_no_roots_offers_no_tools(empty_box):
    assert empty_box.specs() == []
    for name in ("fetch_source", "list_dir", "search", "diff", "run"):
        assert not empty_box.handles(name)


def test_a_toolbox_with_no_roots_refuses_every_call_without_raising(empty_box):
    """An error string is a result the caller can report.  An exception is not.

    StopIteration in particular is not merely an exception here: raised inside a
    generator-driven caller it reads as exhaustion, so the stage ended quietly
    with no adjudication and no fault recorded anywhere.
    """
    for name, args in (("fetch_source", {"path": "x"}),
                       ("list_dir", {"path": ""}),
                       ("search", {"pattern": "x"}),
                       ("diff", {"path": "x"}),
                       ("run", {"argv": ["echo", "hi"]})):
        out = empty_box.dispatch(name, args)
        assert out.startswith("ERROR"), f"{name}: {out}"


def test_resolving_a_path_with_no_roots_is_a_tool_error_not_stopiteration(empty_box):
    with pytest.raises(ToolError, match="no readable roots"):
        empty_box.resolve("anything")


def test_grounding_with_no_roots_reports_rather_than_raising(empty_box):
    """The verifier grounds citations through the same resolve()."""
    ok, note = empty_box.ground("anything", 1, "x")
    assert not ok and note
