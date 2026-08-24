"""A ladder with no credentials refuses before it starts a stage container.

Each driver already raised on an unset key, so a run without one was never scored
zero against the submission -- ``publish_error`` writes ``valid=0`` and says the
grader did not complete.  What it did instead was discover the gap late: the
orchestrator started a stage container, copied State A and the submission in, and
let the stage run toward its own timeout before the first request found the key
missing.  Stage 1 is the gate, so an operator who forgot one export paid a stage
timeout and then read the result as a stage that failed.

Two properties, and the second is the one that decays.  That a missing key is
refused is easy to keep; that the *set* of keys is the one the runners actually
read is not, because stage 3's vendors live in a different file from stage 1's and
a task may add an adversary from either.  So the check is driven from the same
fields the runners read, and one case below asserts a half-configured run --
OpenAI present, Anthropic absent -- is still refused.  That is the realistic
mistake: stage 1 would have passed, and the ladder would have died in stage 3
after hours of stage 2.

Written against ``_preflight_credentials`` and ``_credentials_needed`` rather than
through ``cmd_ladder``: reaching the call site needs a Docker daemon, and a test
that skips without one would assert nothing on the machines that matter.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import config, ladder


KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")

# Stage 1 on one vendor, stage 3 on the other -- the shape all twenty tasks have,
# and the reason one key is not enough to grade with.
EVALUATION = """\
schema = "swerefactor.evaluation/1"
task = "t"
results_dir = "/logs/verifier"

[scoring]
max_score = 100.0
behavioural_points = 40.0
verification_points = 60.0
verification_models = 6
points_per_survived_model = 10.0

[stages.audit]
runner = "audit"
result = "audit.json"
timeout_sec = 5400.0
model = "gpt-5.6-sol"
driver = "openai"
samples = 3

[[stages.audit.gate]]
id = "g"
title = "a gate"
question = "is it ported?"
required = true

[stages.behavioural]
runner = "swerefactor.behavioural"
result = "behavioural.json"
timeout_sec = 10800.0

[stages.verification]
runner = "verification"
result = "verification.json"
probe = "verification/probe.toml"
timeout_sec = 21600.0

[stages.verification.adjudicator]
model = "claude-opus-5"
driver = "anthropic"
"""

PROBE = """\
schema = "swerefactor.verification-probe/1"
task = "t"
candidate_command = ["true"]

[[adversary]]
id = "a01"
model = "claude-opus-5"
driver = "anthropic"

[[adversary]]
id = "a02"
model = "gpt-5.6-terra"
driver = "openai"
"""


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch):
    """The authoring host exports these; a test that read them would pass blind."""
    for name in KEYS:
        monkeypatch.delenv(name, raising=False)


def _load(tmp_path: Path, *, evaluation: str = EVALUATION,
          probe: str | None = PROBE) -> config.Evaluation:
    tests = tmp_path / "tests"
    tests.mkdir(exist_ok=True)
    ev_path = tests / "evaluation.toml"
    ev_path.write_text(evaluation, encoding="utf-8")
    if probe is not None:
        (tests / "verification").mkdir(exist_ok=True)
        (tests / "verification" / "probe.toml").write_text(probe, encoding="utf-8")
    return config.Evaluation.load(ev_path)


# --------------------------------------------------------------------------- #
# what is needed
# --------------------------------------------------------------------------- #

def test_both_vendors_are_named_and_each_says_what_wants_it(tmp_path):
    """Stage 1's driver and stage 3's adversaries and adjudicator all count."""
    needed = ladder._credentials_needed(_load(tmp_path))
    anthropic = "ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN"
    assert set(needed) == {anthropic, "OPENAI_API_KEY"}
    # The adjudicator and the anthropic adversary; the openai one is on the other key.
    assert len(needed[anthropic]) == 2, needed[anthropic]
    assert any("adjudicator" in w for w in needed[anthropic])
    # Stage 1 and the terra adversary.
    assert len(needed["OPENAI_API_KEY"]) == 2, needed["OPENAI_API_KEY"]
    assert any("stage 1" in w for w in needed["OPENAI_API_KEY"])


def test_a_task_naming_its_own_variable_is_honoured(tmp_path):
    """``api_key_env`` is a declared stage option, and the driver reads it first."""
    ev = _load(tmp_path, evaluation=EVALUATION.replace(
        'driver = "openai"\nsamples = 3',
        'driver = "openai"\nsamples = 3\napi_key_env = "MY_OWN_KEY"'))
    needed = ladder._credentials_needed(ev)
    assert "MY_OWN_KEY" in needed
    assert "OPENAI_API_KEY" not in needed or needed["OPENAI_API_KEY"], needed


def test_a_disabled_verification_stage_asks_for_nothing(tmp_path):
    """Stage 3 off is a legal configuration, and it spends no key."""
    ev = _load(tmp_path, evaluation=EVALUATION.replace(
        '[stages.verification]\nrunner = "verification"',
        '[stages.verification]\nenabled = false\nrunner = "verification"'))
    assert set(ladder._credentials_needed(ev)) == {"OPENAI_API_KEY"}


def test_an_unreadable_probe_does_not_raise(tmp_path):
    """Stage 3 complains about its own probe better than a preflight can.

    Refusing to start over a malformed probe would replace a stage's specific
    complaint with a driver's guess at one, and ``validate`` is where that file is
    checked.  So the adjudicator is still counted and the adversaries are skipped.
    """
    ev = _load(tmp_path, probe="this is not toml {{{")
    needed = ladder._credentials_needed(ev)
    assert "OPENAI_API_KEY" in needed              # stage 1, unaffected
    assert any("adjudicator" in w
               for w in needed["ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN"])


# --------------------------------------------------------------------------- #
# what is refused
# --------------------------------------------------------------------------- #

def test_no_keys_at_all_is_refused_and_names_both(tmp_path):
    gap = ladder._preflight_credentials(_load(tmp_path))
    assert gap, "a ladder with no credentials was allowed to start"
    assert "ANTHROPIC_API_KEY" in gap and "OPENAI_API_KEY" in gap
    # The message has to say it is not a judgement about the submission's quality.
    assert "stage 2 needs none" in gap


def test_one_vendor_is_not_enough(tmp_path, monkeypatch):
    """The realistic mistake: stage 1 would pass, and stage 3 would die hours in."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    gap = ladder._preflight_credentials(_load(tmp_path))
    assert gap, "a run that could not finish stage 3 was allowed to start"
    assert "ANTHROPIC_API_KEY" in gap
    assert "OPENAI_API_KEY is unset" not in gap


def test_both_vendors_present_starts(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert ladder._preflight_credentials(_load(tmp_path)) == ""


def test_auth_token_satisfies_anthropic(tmp_path, monkeypatch):
    """A gateway in front of the API usually calls it AUTH_TOKEN, and the driver
    reads both -- so a preflight that demanded API_KEY would refuse a working run."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    assert ladder._preflight_credentials(_load(tmp_path)) == ""


def test_an_empty_key_is_not_a_key(tmp_path, monkeypatch):
    """``export K=`` is the shape a sourced secrets file leaves behind."""
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    gap = ladder._preflight_credentials(_load(tmp_path))
    assert gap and "OPENAI_API_KEY" in gap


# --------------------------------------------------------------------------- #
# against the real tasks
# --------------------------------------------------------------------------- #

def test_every_task_needs_both_vendors(tmp_path):
    """Not a property of the fixture: all twenty split stage 1 and stage 3.

    If a future task grades on one vendor this fails, and the fix is to relax the
    assertion -- but it should be a decision, because "one key is enough" is the
    thing a publisher will assume and the thing that is not true today.
    """
    root = Path(__file__).resolve().parents[2] / "tasks"
    tasks = sorted(p for p in root.iterdir() if (p / "tests" / "evaluation.toml").exists())
    assert len(tasks) >= 20, f"found {len(tasks)} tasks"
    for task in tasks:
        ev = config.Evaluation.load(task / "tests" / "evaluation.toml")
        needed = set(ladder._credentials_needed(ev))
        assert needed == {"ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN",
                          "OPENAI_API_KEY"}, f"{task.name}: {needed}"
        assert ladder._preflight_credentials(ev), f"{task.name} started with no keys"
