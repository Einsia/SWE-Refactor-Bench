"""The `[agent]` block, and why it is read rather than described.

This block was documented as belonging to the runner, so nothing in this
repository read it.  Twenty tasks then declared clocks from 6h to 30h that no
driver consulted: one run received 3h of a declared 30h, stopped with the port
half written, and scored 0.0 -- the same figure a model that cannot do the task
would score.  A declaration nothing reads is worse than no declaration, because
it reads as a control.

The harness is here for the same reason in the other direction.  It is identical
in every task, so declaring it per task looks redundant; what it buys is that a
task states which client its numbers came from, and a suite whose tasks disagree
fails validation instead of producing comparable-looking scores.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import cli, config

GOOD = """
schema_version = "1.4"

[agent]
harness = "codex-cli 0.146.0"
timeout_sec = 32400.0
network_mode = "no-network"
user = "agent"
"""


def task_toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "task.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_pinned_harness_and_a_clock_pass(tmp_path):
    agent = config.AgentPhase.load(task_toml(tmp_path, GOOD))
    assert agent.problems == []
    assert agent.harness == config.PINNED_HARNESS
    assert agent.timeout_sec == 32400.0
    assert agent.user == "agent"


def test_another_harness_is_refused(tmp_path):
    """The first corpus ran five clients. Two of its runs cannot be compared on
    tool use at all, because one of them logged no tool calls."""
    agent = config.AgentPhase.load(
        task_toml(tmp_path, GOOD.replace("codex-cli 0.146.0", "opencode 1.18.13")))
    assert len(agent.problems) == 1
    assert "not one of the pinned clients" in agent.problems[0]
    assert "opencode 1.18.13" in agent.problems[0]


@pytest.mark.parametrize("harness", sorted(config.PINNED_HARNESS_VALUES))
def test_every_pinned_client_is_accepted(tmp_path, harness):
    """Four clients are pinned, one per model family, so a task may declare any of
    them.  All twenty shipped tasks declare the gpt family's, which is also the
    client the archived corpus was driven with -- but the field is checked against
    the set, because which client runs is a property of the run and not of the file."""
    agent = config.AgentPhase.load(
        task_toml(tmp_path, GOOD.replace("codex-cli 0.146.0", harness)))
    assert agent.problems == []
    assert agent.harness == harness


def test_a_version_off_the_pin_is_still_refused(tmp_path):
    """The version is the half of the string that decides comparability: accepting a
    set of clients must not become accepting a client at any version."""
    agent = config.AgentPhase.load(
        task_toml(tmp_path, GOOD.replace("codex-cli 0.146.0", "claude-code 2.1.219")))
    assert len(agent.problems) == 1
    assert "claude-code 2.1.219" in agent.problems[0]


def test_no_harness_at_all_is_refused(tmp_path):
    body = "\n".join(l for l in GOOD.splitlines() if "harness" not in l)
    agent = config.AgentPhase.load(task_toml(tmp_path, body))
    assert any("declares no harness" in p for p in agent.problems)


def test_a_missing_clock_is_refused(tmp_path):
    """Without it the run's entitlement exists only as a flag in somebody's
    shell history."""
    body = "\n".join(l for l in GOOD.splitlines() if "timeout_sec" not in l)
    agent = config.AgentPhase.load(task_toml(tmp_path, body))
    assert any("no timeout_sec" in p for p in agent.problems)


def test_a_nonsense_clock_is_a_config_error_not_a_problem(tmp_path):
    """A negative budget is not a task that scores badly, it is a task that cannot
    be launched, so it raises where the others report."""
    with pytest.raises(config.ConfigError, match="positive"):
        config.AgentPhase.load(
            task_toml(tmp_path, GOOD.replace("32400.0", "-1.0")))
    with pytest.raises(config.ConfigError, match="must be a number"):
        config.AgentPhase.load(
            task_toml(tmp_path, GOOD.replace("32400.0", '"nine hours"')))


def test_the_agent_phase_must_be_offline(tmp_path):
    """Offline is what makes "the old dependency closure is gone" a fact about the
    submitted source rather than a claim about what was downloaded."""
    agent = config.AgentPhase.load(
        task_toml(tmp_path, GOOD.replace('"no-network"', '"bridge"')))
    assert any("offline" in p for p in agent.problems)


def test_a_task_with_no_agent_block_says_so(tmp_path):
    with pytest.raises(config.ConfigError, match=r"no \[agent\] block"):
        config.AgentPhase.load(task_toml(tmp_path, 'schema_version = "1.4"\n'))


def test_validate_reports_the_phase_and_its_problems(tmp_path):
    """`_agent_phase` is what puts it in front of a task author, and it notes the
    figures on the way past so a wrong-but-valid clock is visible too."""
    notes: list[str] = []
    task_toml(tmp_path, GOOD)
    assert cli._agent_phase(tmp_path, notes) == []
    assert notes == ["agent phase: codex-cli 0.146.0, 9.0h, no-network"]

    notes.clear()
    task_toml(tmp_path, GOOD.replace("codex-cli 0.146.0", "claude-code"))
    problems = cli._agent_phase(tmp_path, notes)
    assert len(problems) == 1 and notes == []


def test_a_directory_without_a_task_toml_is_not_this_check_s_business(tmp_path):
    """validate already reports a missing task.toml once, by name."""
    assert cli._agent_phase(tmp_path, []) == []


@pytest.mark.parametrize("old,new,names", [
    # The clock is the whole reason this reader exists, and `timeout_secs` is the
    # spelling every other block in the tree uses for something.  Misspelled, it
    # reads as a task that declares no clock -- which is the state the corpus was
    # already in, and the one this block was added to end.
    ("timeout_sec = 32400.0", "timeout_secs = 32400.0", "timeout_sec"),
    ("harness = ", "harnes = ", "harness"),
    ("network_mode = ", "network_moed = ", "network_mode"),
])
def test_a_misspelled_key_is_named_and_not_defaulted(tmp_path, old, new, names):
    with pytest.raises(config.ConfigError) as exc:
        config.AgentPhase.load(task_toml(tmp_path, GOOD.replace(old, new)))
    msg = str(exc.value)
    assert "unknown key" in msg and names in msg, msg


def test_a_key_from_the_runner_s_vocabulary_is_rejected_too():
    """Not only typos.  `[agent]` was documented as the runner's, so its plausible
    keys are the runner's: `cpus`, `memory_mb`, `image`.  Each is a claim the file
    makes about the run, and nothing here honours any of them."""
    assert "cpus" not in config.AgentPhase.KNOWN
    assert "memory_mb" not in config.AgentPhase.KNOWN
    # Four keys read, four keys the shipped tasks write, and no bag to absorb a
    # fifth -- so the two lists cannot drift apart without a test failing.
    assert set(config.AgentPhase.KNOWN) == {
        "harness", "timeout_sec", "network_mode", "user"}


def test_the_key_list_does_not_travel_inside_the_object(tmp_path):
    """`KNOWN` is a ClassVar, so it is not a field: it stays off `__init__` and out
    of equality, where a tuple of key names would compare as data."""
    agent = config.AgentPhase.load(task_toml(tmp_path, GOOD))
    assert "KNOWN" not in {f.name for f in dataclasses.fields(agent)}
    assert agent == config.AgentPhase.load(task_toml(tmp_path, GOOD))


def test_every_shipped_task_declares_a_pinned_client():
    """The check that would have caught the first corpus's five clients.

    Every shipped task declares `codex-cli 0.146.0`, which is the gpt family's pin
    and the client the archived corpus was driven with.  The assertion is membership
    in the four pins rather than equality with one of them, because a task authored
    for another family would name that family's client and be equally correct here --
    `[agent].harness` says what the corpus was collected with, and this file cannot
    see which model a future run will use.  Pairing the two is the adapter's job: it
    refuses to launch a client whose version is not its own family's pin.
    """
    tasks = sorted((Path(__file__).resolve().parents[2] / "tasks").glob("*/task.toml"))
    assert len(tasks) >= 20
    for path in tasks:
        agent = config.AgentPhase.load(path)
        assert agent.problems == [], f"{path.parent.name}: {agent.problems}"
