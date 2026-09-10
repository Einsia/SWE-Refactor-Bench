"""The audit review: grounding, voting, and what happens when it cannot tell.

The review is a model's opinion given the power to zero a hundred-point task, so
these tests are about the mechanisms that keep that from being arbitrary --
citations are checked against the tree, several samples vote, and a fail that is
not carried by evidence and a majority resolves to pass.

There is no abstention: `fail` is the verdict that has to be earned and everything
else lands on `pass`, because an undecided *required* gate stops the ladder as a
harness error and throws away stage 2's measured points.  `error` survives for one
case, and it is not a verdict about the submission -- no review returned a usable
answer at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import agentloop, audit, models
from swerefactor.config import Gate
from swerefactor.tools import toolbox_for


@pytest.fixture
def trees(tmp_path: Path) -> tuple[Path, Path]:
    """A minimal original/workspace pair with one honest file and one shim."""
    original = tmp_path / "original"
    workspace = tmp_path / "workspace"
    (original / "src").mkdir(parents=True)
    (workspace / "src").mkdir(parents=True)
    (original / "src" / "render.c").write_text(
        "#include <stdio.h>\n"
        "void render(const char *s) {\n"
        "    printf(\"%s\", s);\n"
        "}\n", encoding="utf-8")
    (workspace / "src" / "render.rs").write_text(
        "pub fn render(s: &str) {\n"
        "    print!(\"{}\", s);\n"
        "}\n", encoding="utf-8")
    (workspace / "src" / "shim.rs").write_text(
        "// still calling the C library\n"
        "extern \"C\" { fn render(s: *const c_char); }\n", encoding="utf-8")
    return original, workspace


def gate(gid: str = "no_shim", required: bool = True) -> Gate:
    return Gate(id=gid, title=gid, question="Did the old implementation leave?",
                required=required)


def build(trees, samples: int, turns_per_sample: list[list[dict]]):
    """A verifier whose model replies from a script, one script per sample."""
    original, workspace = trees
    toolbox = toolbox_for(original, workspace)
    engine = audit.Auditor(
        task="t", gates=[gate()], prompt="Review {{task}}.\n{{gates}}",
        toolbox=toolbox, samples=samples)
    scripts = iter(turns_per_sample)

    def factory(_index: int) -> models.Driver:
        return models.build("scripted", "scripted-model",
                            options={"turns": next(scripts)})
    return engine, factory


def submission(verdict: str, evidence: list[dict] | None = None,
               gid: str = "no_shim", reasoning: str = "because of the code"):
    """One scripted turn that calls submit_review with the given verdict."""
    return [{
        "tool_calls": [{
            "name": "submit_review",
            "arguments": {
                "summary": "reviewed",
                "gates": [{"id": gid, "verdict": verdict,
                           "confidence": 0.9, "reasoning": reasoning,
                           "evidence": evidence or []}],
            },
        }],
    }]


# --------------------------------------------------------------------------- #
# grounding
# --------------------------------------------------------------------------- #


def test_a_fail_with_a_real_citation_fails_the_gate(trees):
    engine, factory = build(trees, 1, [submission("fail", [
        {"path": "workspace:src/shim.rs", "line": 2,
         "quote": "extern \"C\"", "note": "still calling C"},
    ])])
    res = engine.run(factory)
    check = res.checks[0]
    assert check.verdict == "fail"
    assert check.evidence[0]["grounded"] is True


def test_a_fail_whose_citation_does_not_exist_becomes_a_pass(trees):
    """An accusation that cannot be checked does not get to zero a submission."""
    engine, factory = build(trees, 1, [submission("fail", [
        {"path": "workspace:src/nonexistent.rs", "line": 12, "quote": "shim"},
    ])])
    res = engine.run(factory)
    check = res.checks[0]
    assert check.verdict == "pass"           # not a fail, and not undecided
    assert "did not check out" in json.dumps(res.metadata)
    assert check.metadata["defaulted_to_pass"]   # and it says so


def test_a_citation_past_the_end_of_a_file_does_not_ground(trees):
    engine, factory = build(trees, 1, [submission("fail", [
        {"path": "workspace:src/render.rs", "line": 9000, "quote": "anything"},
    ])])
    res = engine.run(factory)
    assert res.checks[0].verdict == "pass"


def test_a_quote_that_is_not_in_the_file_does_not_ground(trees):
    engine, factory = build(trees, 1, [submission("fail", [
        {"path": "workspace:src/render.rs", "line": 1,
         "quote": "import flask"},
    ])])
    res = engine.run(factory)
    assert res.checks[0].verdict == "pass"


def test_a_quote_reflowed_by_the_model_still_grounds(trees):
    """Whitespace differences are not a reason to discard a true citation."""
    engine, factory = build(trees, 1, [submission("fail", [
        {"path": "workspace:src/shim.rs", "line": 2,
         "quote": "extern   \"C\"   {   fn render"},
    ])])
    res = engine.run(factory)
    assert res.checks[0].verdict == "fail"


def test_a_pass_needs_no_evidence(trees):
    engine, factory = build(trees, 1, [submission("pass")])
    res = engine.run(factory)
    assert res.checks[0].verdict == "pass"


# --------------------------------------------------------------------------- #
# voting
# --------------------------------------------------------------------------- #


def evidence():
    return [{"path": "workspace:src/shim.rs", "line": 1,
             "quote": "still calling the C library"}]


def test_the_majority_carries(trees):
    engine, factory = build(trees, 3, [
        submission("fail", evidence()),
        submission("fail", evidence()),
        submission("pass"),
    ])
    res = engine.run(factory)
    check = res.checks[0]
    assert check.verdict == "fail"
    assert check.metadata["votes"] == {"pass": 1, "fail": 2}


def test_a_single_dissenting_fail_does_not_carry(trees):
    engine, factory = build(trees, 3, [
        submission("pass"),
        submission("pass"),
        submission("fail", evidence()),
    ])
    res = engine.run(factory)
    assert res.checks[0].verdict == "pass"


def test_a_tie_passes_and_says_it_was_a_tie(trees):
    """A tie needs an even quorum, so it needs a dead review out of three.

    It resolves the way every other split does -- a fail is carried by a majority
    or it is not carried -- rather than as `error`, which on a required gate would
    stop the ladder and discard stage 2's measured points.  The metadata flag is
    the part that matters: a 1-1 pass is a weaker pass than a 3-0.
    """
    engine, factory = build(trees, 2, [
        submission("pass"),
        submission("fail", evidence()),
    ])
    res = engine.run(factory)
    check = res.checks[0]
    assert check.verdict == "pass"
    assert check.metadata["tie_broken_to_pass"] is True
    assert "split" in check.summary


def test_an_unrecognised_verdict_is_sent_back_rather_than_counted(trees):
    """'abstain' is not an answer, and the model is told rather than guessed for.

    The submission check is where a third answer is caught: a review that sent
    'abstain' meant something by it, and asking beats picking for it.  A review that
    corrects itself votes; one that repeats itself spends its budget and does not
    vote, which is the same place a review that never submitted ends up -- not a
    failure of the submission.
    """
    engine, factory = build(trees, 2, [
        submission("abstain") + submission("pass"),      # asked again, corrects
        submission("abstain") + submission("abstain"),   # asked again, repeats
    ])
    res = engine.run(factory)
    check = res.checks[0]
    assert check.verdict == "pass"
    assert check.metadata["votes"] == {"pass": 1, "fail": 0}
    assert check.metadata["samples_usable"] == 1
    assert check.metadata["samples_run"] == 2


def test_the_resubmit_message_names_the_two_allowed_answers(trees):
    """What the model is actually told, since only the transcript records it."""
    complaint = audit._check_submission(
        {"gates": [{"id": "no_shim", "verdict": "abstain", "reasoning": "unsure"}]},
        [gate()])
    assert complaint is not None
    assert "not one of the two allowed answers" in complaint
    assert "no_shim (sent 'abstain')" in complaint


def test_a_verdict_the_schema_does_not_define_is_not_a_failure(trees):
    """`_vote` is the last thing before a zero, so it must not fail on a stray word.

    Unreachable through the loop -- the submission check turns it away and keeps
    doing so.  Reached directly, because that is the point of the branch.
    """
    original, workspace = trees
    engine = audit.Auditor(task="t", gates=[gate()], prompt="{{gates}}",
                               toolbox=toolbox_for(original, workspace), samples=1)
    vote = engine._vote({"id": "no_shim", "verdict": "abstain", "reasoning": "r"})
    assert vote.verdict == "pass"
    assert "not one of" in vote.downgraded


@pytest.mark.parametrize("key", ["root", "file", "location"])
def test_a_citation_grounds_under_the_key_models_actually_use(trees, key):
    """The schema asks for 'path'; reviews write 'root' and the finding is real.

    fw02's own run: two of three reviews keyed every citation of every gate
    'root', invited by the schema's description of the *value* ("root:relative
    path").  Dropping them turned a grounded fail into "no citations", which is
    the one message that means the opposite of what happened.
    """
    engine, factory = build(trees, 1, [submission("fail", [
        {key: "workspace:src/shim.rs", "line": 1,
         "quote": "still calling the C library"},
    ])])
    res = engine.run(factory)
    check = res.checks[0]
    assert check.verdict == "fail"
    assert check.evidence[0]["grounded"] is True


def test_a_pathless_citation_is_sent_back_rather_than_dropped(trees):
    """Evidence with no usable path key is a fail with no evidence, later."""
    original, workspace = trees
    toolbox = toolbox_for(original, workspace)
    engine = audit.Auditor(task="t", gates=[gate()], prompt="{{gates}}",
                               toolbox=toolbox, samples=1)
    turns = [
        {"tool_calls": [{"name": "submit_review", "arguments": {
            "summary": "s", "gates": [{"id": "no_shim", "verdict": "fail",
                                       "reasoning": "it is all wrong",
                                       "evidence": [{"line": 1,
                                                     "quote": "no path"}]}]}}]},
        {"tool_calls": [{"name": "submit_review", "arguments": {
            "summary": "s", "gates": [{"id": "no_shim", "verdict": "fail",
                                       "reasoning": "here it is",
                                       "evidence": evidence()}]}}]},
    ]
    driver = models.build("scripted", "m", options={"turns": turns})
    res = engine.run(lambda _i: driver)
    assert res.checks[0].verdict == "fail"


def test_a_review_that_never_submitted_does_not_vote(trees):
    engine, factory = build(trees, 3, [
        [{"text": "I had a look and I have opinions."},
         {"text": "Still just talking."}],           # nudged, then abandoned
        submission("pass"),
        submission("pass"),
    ])
    res = engine.run(factory)
    check = res.checks[0]
    assert check.verdict == "pass"
    assert check.metadata["samples_usable"] == 2
    assert check.metadata["samples_run"] == 3


def test_no_usable_review_marks_the_stage_errored(trees):
    engine, factory = build(trees, 1, [[{"text": "nothing to say"},
                                        {"text": "still nothing"}]])
    res = engine.run(factory)
    assert res.status == "error"
    assert res.checks[0].verdict == "error"


# --------------------------------------------------------------------------- #
# the submission contract
# --------------------------------------------------------------------------- #


def test_a_missing_gate_verdict_is_sent_back_once(trees):
    """The model gets told what it left out rather than having it inferred."""
    original, workspace = trees
    toolbox = toolbox_for(original, workspace)
    engine = audit.Auditor(
        task="t", gates=[gate("a"), gate("b")], prompt="{{gates}}",
        toolbox=toolbox, samples=1)
    turns = [
        {"tool_calls": [{"name": "submit_review", "arguments": {
            "summary": "half of it",
            "gates": [{"id": "a", "verdict": "pass", "reasoning": "fine"}]}}]},
        {"tool_calls": [{"name": "submit_review", "arguments": {
            "summary": "all of it",
            "gates": [{"id": "a", "verdict": "pass", "reasoning": "fine"},
                      {"id": "b", "verdict": "pass", "reasoning": "also fine"}]}}]},
    ]
    driver = models.build("scripted", "m", options={"turns": turns})
    res = engine.run(lambda _i: driver)
    assert [c.verdict for c in res.checks] == ["pass", "pass"]


def test_an_unevidenced_fail_is_sent_back_before_it_is_accepted(trees):
    original, workspace = trees
    toolbox = toolbox_for(original, workspace)
    engine = audit.Auditor(task="t", gates=[gate()], prompt="{{gates}}",
                               toolbox=toolbox, samples=1)
    turns = [
        {"tool_calls": [{"name": "submit_review", "arguments": {
            "summary": "s", "gates": [{"id": "no_shim", "verdict": "fail",
                                       "reasoning": "it feels wrong"}]}}]},
        {"tool_calls": [{"name": "submit_review", "arguments": {
            "summary": "s", "gates": [{"id": "no_shim", "verdict": "fail",
                                       "reasoning": "here it is",
                                       "evidence": evidence()}]}}]},
    ]
    driver = models.build("scripted", "m", options={"turns": turns})
    res = engine.run(lambda _i: driver)
    assert res.checks[0].verdict == "fail"


# --------------------------------------------------------------------------- #
# what counts as naming a file
# --------------------------------------------------------------------------- #
# The field is `path` and its description asks for a "root:relative path", which a
# sample can read as the name of the field rather than as a file to name.  A
# citation like that carries no file, so grounding has nothing to check it against
# and drops it -- and an entry whose evidence was dropped is indistinguishable from
# one that cited nothing.  Hence the two tests below: what counts as naming a file
# is decided here, before grounding, where the sample can still be asked again.


def test_evidence_keyed_root_still_names_a_file(trees):
    """A citation is not lost because the field holding it was misnamed."""
    engine, factory = build(trees, 1, [submission("fail", [
        {"root": "workspace:src/shim.rs", "line": 2, "quote": "extern \"C\""},
    ])])
    res = engine.run(factory)
    check = res.checks[0]
    assert check.verdict == "fail"
    assert check.evidence[0]["grounded"] is True
    assert check.evidence[0]["path"] == "workspace:src/shim.rs"


def test_a_bare_root_name_is_not_a_citation(trees):
    """`workspace` is a root, not a file, and must not be promoted to one.

    Grounding would reject it anyway, but as "quoted text not found" -- which
    reads as a model citing something untrue rather than one that never said
    where to look.
    """
    engine, factory = build(trees, 1, [submission("fail", [
        {"root": "workspace", "line": 2, "quote": "extern \"C\""},
    ])])
    res = engine.run(factory)
    # Not a fail, which is the point.  `error` is the sample's own outcome, not a
    # verdict about the tree: the submission check sent this back for a usable
    # path and the one-turn script had no second reply, so nothing voted.
    assert res.checks[0].verdict == "error"
    assert res.checks[0].metadata["samples_usable"] == 0
    # What the model was told, which only the transcript records.
    msg = audit._check_submission(
        {"gates": [{"id": "no_shim", "verdict": "fail", "reasoning": "r",
                    "evidence": [{"root": "workspace", "line": 2,
                                  "quote": "extern \"C\""}]}]},
        [gate()])
    assert msg and "Evidence naming no file" in msg and "root" in msg


def test_an_unusable_citation_is_not_reported_as_an_absent_one(trees):
    """The downgrade reason has to distinguish the two, or the fix is invisible.

    Reached directly: the submission check now turns such a fail away at the
    door, so the loop cannot deliver one.  The branch stays because `_vote` is
    the last thing between an accusation and a zeroed submission, and reporting
    five discarded citations as "no citations" is what hid this for two runs.
    """
    original, workspace = trees
    engine = audit.Auditor(task="t", gates=[gate()], prompt="{{gates}}",
                               toolbox=toolbox_for(original, workspace),
                               samples=1)
    vote = engine._vote({"id": "no_shim", "verdict": "fail", "reasoning": "r",
                         "evidence": [{"file": "src/shim.rs", "line": 2},
                                      {"root": "workspace"}]})
    assert vote.verdict == "pass"
    assert "none naming a file" in vote.downgraded
    assert "no citations" not in vote.downgraded


def test_a_partly_unusable_evidence_list_still_grounds_on_the_rest(trees):
    """One misnamed entry among several must not sink the ones that are fine."""
    engine, factory = build(trees, 1, [submission("fail", [
        {"file": "src/shim.rs", "line": 2},
        {"path": "workspace:src/shim.rs", "line": 2, "quote": "extern \"C\""},
    ])])
    res = engine.run(factory)
    assert res.checks[0].verdict == "fail"


def test_a_fail_whose_evidence_names_no_file_is_sent_back(trees):
    """The one re-ask has to fire here too, not only on an empty list.

    An entry that names no file is as ungroundable as no entry at all, so testing
    emptiness alone lets a fail through the door to be dropped on the far side,
    with no chance to correct it.
    """
    original, workspace = trees
    toolbox = toolbox_for(original, workspace)
    engine = audit.Auditor(task="t", gates=[gate()], prompt="{{gates}}",
                               toolbox=toolbox, samples=1)
    turns = [
        {"tool_calls": [{"name": "submit_review", "arguments": {
            "summary": "s", "gates": [{"id": "no_shim", "verdict": "fail",
                                       "reasoning": "it is there",
                                       "evidence": [{"file": "src/shim.rs",
                                                     "line": 2}]}]}}]},
        {"tool_calls": [{"name": "submit_review", "arguments": {
            "summary": "s", "gates": [{"id": "no_shim", "verdict": "fail",
                                       "reasoning": "here it is",
                                       "evidence": evidence()}]}}]},
    ]
    driver = models.build("scripted", "m", options={"turns": turns})
    res = engine.run(lambda _i: driver)
    assert res.checks[0].verdict == "fail"


def test_the_reask_names_the_field_that_arrived_and_the_one_to_use(trees):
    """One reply has to be enough: the line and the quote are usually right."""
    msg = audit._check_submission(
        {"summary": "s", "gates": [{"id": "no_shim", "verdict": "fail",
                                    "reasoning": "r",
                                    "evidence": [{"file": "src/shim.rs"}]}]},
        [gate()])
    assert msg and "file" in msg and '"path"' in msg


def test_no_reask_when_the_evidence_is_already_usable(trees):
    """Re-asking for what is already in hand spends the one correction."""
    assert audit._check_submission(
        {"summary": "s", "gates": [{"id": "no_shim", "verdict": "fail",
                                    "reasoning": "r",
                                    "evidence": [{"root": "workspace:src/shim.rs",
                                                  "line": 2}]}]},
        [gate()]) is None


# --------------------------------------------------------------------------- #
# prompt rendering
# --------------------------------------------------------------------------- #


def test_placeholders_are_filled_and_gates_are_listed(trees):
    original, workspace = trees
    toolbox = toolbox_for(original, workspace)
    text = audit.render_prompt(
        "task={{task}} orig={{original}} ws={{workspace}}\n{{gates}}",
        task="lang01", gates=[gate("no_shim"), gate("deps", required=False)],
        toolbox=toolbox)
    assert "task=lang01" in text
    assert str(original) in text and str(workspace) in text
    assert "[no_shim]  (REQUIRED)" in text
    assert "[deps]  (advisory)" in text


def test_an_unknown_placeholder_is_left_visible(trees):
    original, workspace = trees
    text = audit.render_prompt("{{nosuchthing}}", task="t", gates=[],
                                  toolbox=toolbox_for(original, workspace))
    assert text == "{{nosuchthing}}"
