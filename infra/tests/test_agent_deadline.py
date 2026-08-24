"""Whether the loop stops itself before Harbor kills it.

Harbor ends the agent phase by raising ``AgentTimeoutError`` through the exec at the
task's declared ``[agent] timeout_sec``.  The loop dies mid-round having written
nothing, so ``agent/stop-reason.txt`` never appears -- and a reader of the phase has
no stop reason to attribute the ending to, only its absence.  A run cut by the wall
recorded no measurement, however much work it had done.

So the loop has to reach its own deadline first, and that deadline has to be derived
from the task's own declared wall rather than set as a constant.  The twenty walls in
this suite span 21600s to 108000s: any single ceiling is either above some of them,
in which case Harbor fires first and the loop's own stop reasons are never written,
or below others, in which case it spends less of the wall than the task declared.

The property is therefore per task, which is what the load-bearing assertion here is
shaped by: it runs against the real ``task.toml`` files, and for every task in the
suite the derived deadline must come in strictly under that task's declared wall.

Every case runs against both adapters.  ``harbor_agent.SrbCodex`` and
``harbor_claude.SrbClaudeCode`` drive the same three limits over the same twenty
walls, so a claim proved on one of them is untested on the client the other family is
measured with.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[1]
REPO = INFRA.parent
sys.path.insert(0, str(INFRA))


def _stub_harbor() -> None:
    """Satisfy the seven Harbor imports so the module can be imported at all.

    ``harbor_agent`` names Harbor symbols at import time -- ``CliFlag`` is *called*
    in the class body -- and Harbor lives in its own interpreter at
    /opt/uv-tools/harbor, which has no pytest.  Skipping instead would mean a test
    that runs nowhere: not in this interpreter for want of Harbor, and not in
    Harbor's for want of pytest.

    Import-satisfying only, and deliberately not a fake of any behaviour: nothing
    under test touches these.  ``_deadline_sec`` reads a JSON file, a TOML file and
    one env lookup, which is exactly why it can be tested without a trial at all.
    Used only when the real package is absent, so an environment that has Harbor
    tests against Harbor.
    """
    def register(name: str, **attrs) -> None:
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            dotted = ".".join(parts[:i])
            sys.modules.setdefault(dotted, types.ModuleType(dotted))
            if i > 1:
                setattr(sys.modules[".".join(parts[:i - 1])], parts[i - 1],
                        sys.modules[dotted])
        for key, value in attrs.items():
            setattr(sys.modules[name], key, value)

    placeholder = lambda name: type(name, (), {})              # noqa: E731
    register("harbor.agents.installed.base",
             CliFlag=lambda *a, **k: None)
    register("harbor.agents.installed.codex", Codex=placeholder("Codex"))
    register("harbor.agents.installed.claude_code",
             ClaudeCode=placeholder("ClaudeCode"))
    register("harbor.environments.base",
             BaseEnvironment=placeholder("BaseEnvironment"))
    register("harbor.models.agent.context",
             AgentContext=placeholder("AgentContext"))
    register("harbor.models.task.config", NetworkMode=placeholder("NetworkMode"),
             NetworkPolicy=placeholder("NetworkPolicy"))
    register("harbor.models.trial.paths",
             EnvironmentPaths=placeholder("EnvironmentPaths"))
    register("harbor.utils.trajectory_utils",
             format_trajectory_json=lambda *a, **k: "")


try:
    import harbor  # noqa: F401
except ModuleNotFoundError:
    _stub_harbor()

from swerefactor import harbor_agent, harbor_claude, tomlcompat  # noqa: E402

CEILING = 144000
HEADROOM = harbor_agent.SrbCodex._WALL_HEADROOM_SEC

#: Both adapters drive the same three ordered limits and both are launched against
#: the same twenty task files, so every assertion below runs against both.  The
#: Claude adapter kept the flat 144000 ceiling for its whole life precisely because
#: this module named only the codex class: the defect and its test had the same
#: blind spot, and a fix verified on one adapter says nothing about the other.
ADAPTERS = [harbor_agent.SrbCodex, harbor_claude.SrbClaudeCode]
ADAPTER_IDS = [c.__name__ for c in ADAPTERS]

assert harbor_claude.SrbClaudeCode._WALL_HEADROOM_SEC == HEADROOM, (
    "the two adapters must leave the loop the same headroom, or a run's stop "
    "reason depends on which client drove it"
)


def _agent(tmp_path, monkeypatch, wall=None, *, env=None, override=None,
           task_rel="tasks/t1", config=True, toml=True, cls=harbor_agent.SrbCodex):
    """A trial directory as Harbor lays one out, and an agent pointed at it.

    Built with ``object.__new__`` rather than the constructor: instantiating for
    real would need Harbor's own ``Codex.__init__`` and a live environment, and
    would put the wiring under test instead of the arithmetic.  What
    ``_deadline_sec`` needs from an instance is three things -- an override, a
    ``logs_dir`` and ``_get_env`` -- so those are what it gets.

    ``task.path`` is written relative and the cwd is moved to the fake repo root,
    because that is the form Harbor writes and the form ``.resolve()`` has to
    handle.
    """
    trial = tmp_path / "trial"
    (trial / "logs").mkdir(parents=True, exist_ok=True)
    if config:
        (trial / "config.json").write_text(
            json.dumps({"task": {"path": task_rel}, "model": "gpt-5.6-sol"}),
            encoding="utf-8")
    if wall is not None and toml:
        task_dir = tmp_path / task_rel
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.toml").write_text(
            f"[agent]\ntimeout_sec = {wall}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    agent = object.__new__(cls)
    agent._deadline_override = override
    agent.logs_dir = trial / "logs"
    agent._get_env = lambda name: (env or {}).get(name)
    return agent


@pytest.mark.parametrize("cls", ADAPTERS, ids=ADAPTER_IDS)
def test_the_deadline_comes_in_under_the_declared_wall(tmp_path, monkeypatch, cls):
    """The whole fix: derived from the task's wall, not from a fixed ceiling."""
    agent = _agent(tmp_path, monkeypatch, wall=21600, cls=cls)
    assert agent._deadline_sec() == 21600 - HEADROOM


@pytest.mark.parametrize("cls", ADAPTERS, ids=ADAPTER_IDS)
def test_the_three_limits_end_up_ordered(tmp_path, monkeypatch, cls):
    """Loop, then exec, then Harbor -- in that order, with the loop first.

    ``run()`` gives the exec ``deadline + 600``, and Harbor's wait_for fires at the
    wall.  Whichever of the outer two ever fires, it must fire after the loop has
    already written its stop reason; otherwise the phase ends with nothing to say
    about itself and the score is dropped.  Before the fix all three were the same
    limit in the wrong order.
    """
    wall = 21600
    deadline = _agent(tmp_path, monkeypatch, wall=wall, cls=cls)._deadline_sec()
    loop_timeout = deadline + 600           # both adapters' exec headroom
    assert deadline < loop_timeout < wall, (deadline, loop_timeout, wall)


@pytest.mark.parametrize("cls", ADAPTERS, ids=ADAPTER_IDS)
def test_every_task_in_the_suite_stops_itself_before_harbor_does(tmp_path,
                                                                monkeypatch, cls):
    """Against the real task files, because the defect was about all of them.

    A ceiling above every declared wall in the suite is not wrong for one task, it is
    wrong for all twenty at once: the loop can never reach its own deadline first,
    whatever it was measuring.  Parameterising over the actual ``task.toml`` files is
    what makes that statement checkable rather than recalled.
    """
    walls = []
    for toml_path in sorted((REPO / "tasks").glob("*/task.toml")):
        wall = (tomlcompat.load(toml_path).get("agent") or {}).get("timeout_sec")
        if wall:
            walls.append((toml_path.parent.name, float(wall)))
    # A glob that matched nothing would otherwise pass as "every task is fine".
    assert len(walls) >= 15, f"only found {len(walls)} declared wall(s)"

    for name, wall in walls:
        agent = _agent(tmp_path, monkeypatch, wall=wall, task_rel=f"tasks/{name}",
                       cls=cls)
        deadline = agent._deadline_sec()
        assert deadline + 600 < wall, (
            f"{name}: deadline {deadline}s + exec headroom is not under its "
            f"{wall}s wall, so Harbor would cut the loop before it could write "
            f"a stop reason"
        )
        assert deadline < CEILING, f"{name}: still using the flat ceiling"


# --------------------------------------------------------------------------- #
# the fallbacks: nothing here may make a launch worse than it was
# --------------------------------------------------------------------------- #
#
# Reading the wall is best-effort by design.  This runs at launch, and a deadline
# is not worth failing a phase over: every one of these cases has to land on the
# old ceiling, which is exactly the behaviour every run before the fix had.

@pytest.mark.parametrize("cls", ADAPTERS, ids=ADAPTER_IDS)
@pytest.mark.parametrize("case,kwargs", [
    ("no config.json at all", dict(config=False, wall=21600)),
    ("config names no task", dict(task_rel="", wall=21600)),
    ("task.toml is not there", dict(wall=21600, toml=False)),
    ("task declares no wall", dict(wall=None)),
])
def test_a_wall_it_cannot_read_keeps_the_old_ceiling(tmp_path, monkeypatch,
                                                    case, kwargs, cls):
    agent = _agent(tmp_path, monkeypatch, cls=cls, **kwargs)
    assert agent._deadline_sec() == CEILING, case


@pytest.mark.parametrize("cls", ADAPTERS, ids=ADAPTER_IDS)
def test_an_unreadable_config_does_not_fail_the_launch(tmp_path, monkeypatch, cls):
    """Malformed JSON is a caught exception, not a traceback out of launch."""
    agent = _agent(tmp_path, monkeypatch, wall=21600, cls=cls)
    (tmp_path / "trial" / "config.json").write_text("{not json", encoding="utf-8")
    assert agent._deadline_sec() == CEILING


@pytest.mark.parametrize("cls", ADAPTERS, ids=ADAPTER_IDS)
def test_a_wall_too_short_for_the_headroom_keeps_the_ceiling(tmp_path, monkeypatch,
                                                            cls):
    """Subtracting 900s from a 1200s wall leaves a quarter of it to work in.

    The headroom is slack taken from a budget of hours; on a wall that short it
    would dominate rather than trim, so the derivation declines and the old
    behaviour stands.
    """
    agent = _agent(tmp_path, monkeypatch, wall=HEADROOM * 2, cls=cls)
    assert agent._deadline_sec() == CEILING


@pytest.mark.parametrize("cls", ADAPTERS, ids=ADAPTER_IDS)
def test_the_env_override_still_wins_over_a_declared_wall(tmp_path, monkeypatch, cls):
    """An operator who set the variable meant it; the wall is only the default."""
    agent = _agent(tmp_path, monkeypatch, wall=21600, cls=cls,
                   env={"SRB_AGENT_DEADLINE_SEC": "1234"})
    assert agent._deadline_sec() == 1234


@pytest.mark.parametrize("cls", ADAPTERS, ids=ADAPTER_IDS)
def test_the_constructor_override_wins_over_both(tmp_path, monkeypatch, cls):
    agent = _agent(tmp_path, monkeypatch, wall=21600, override=99, cls=cls,
                   env={"SRB_AGENT_DEADLINE_SEC": "1234"})
    assert agent._deadline_sec() == 99


#: Denied at both clients.
DENIED_TOOLS = {"WebSearch", "WebFetch"}

CLAUDE_LOOP = Path(harbor_claude._LOOP_SCRIPT)


def test_the_codex_config_disables_the_hosted_web_tool():
    """Rendered and parsed, not matched as template text.

    ``web_search`` is validated by the client: an unknown key is accepted in
    silence, so a misspelling would disable nothing and report nothing.
    """
    agent = object.__new__(harbor_agent.SrbCodex)
    agent.model_name = "gpt-5.6-sol"
    agent._resolved_flags = {"reasoning_effort": "high"}
    agent._gateway_base_url = lambda: "https://gateway.invalid/v1"

    config = tomlcompat.loads(agent._render_config())
    assert config.get("web_search") == "disabled", (
        f"codex config has web_search={config.get('web_search')!r}; absent means "
        f"the client's own default applies"
    )


def test_the_claude_settings_deny_the_web_tools_by_bare_name():
    """A bare name keeps the tool out of what the model is offered.

    A scoped rule only refuses calls, and deny outranks any later allow, so this
    holds for an invocation that asks for the tools explicitly.
    """
    settings = json.loads(harbor_claude._SETTINGS_JSON)
    deny = settings["permissions"]["deny"]
    assert set(deny) == DENIED_TOOLS, deny
    assert all("(" not in rule for rule in deny), deny


def test_both_places_that_name_the_denied_tools_agree():
    """The adapter's settings and the loop's command line are one policy.

    Two declarations, because they cover different invocations: the settings file
    covers anything reading that config dir, the command line covers the loop's own
    round.  A tool named in one and not the other is offered by whichever the run
    goes through.
    """
    line = [ln for ln in CLAUDE_LOOP.read_text(encoding="utf-8").splitlines()
            if ln.startswith("DENIED_TOOLS=")]
    assert len(line) == 1, f"expected one DENIED_TOOLS assignment, found {line}"
    in_loop = set(line[0].split("=", 1)[1].strip('"').split(","))
    assert in_loop == set(harbor_claude._DENIED_TOOLS) == DENIED_TOOLS, (
        f"loop denies {sorted(in_loop)}, adapter denies "
        f"{sorted(harbor_claude._DENIED_TOOLS)}"
    )


def _installed_commands(monkeypatch) -> list[str]:
    """Every root command ``SrbClaudeCode.install`` issues, in order.

    Driven through the real method rather than read off the source: a constant can
    be declared and never written, and only the install path says which it is.
    """
    agent = object.__new__(harbor_claude.SrbClaudeCode)
    agent._version = harbor_claude.PINNED_VERSION
    agent.parse_version = lambda text: harbor_claude.PINNED_VERSION
    issued: list[str] = []

    async def exec_as_root(environment, command):
        issued.append(command)

    agent.exec_as_root = exec_as_root
    monkeypatch.setattr(harbor_claude, "EnvironmentPaths",
                        types.SimpleNamespace(agent_dir=Path("/logs/agent")))

    class Env:
        async def exec(self, command):
            return types.SimpleNamespace(
                return_code=0, stdout=harbor_claude.PINNED_VERSION)

        async def upload_file(self, src, dst):
            issued.append(f"upload {dst}")

    asyncio.run(agent.install(Env()))
    return issued


def test_install_writes_the_claude_settings_into_the_container(monkeypatch):
    """The deny has to reach the config dir the client reads, not just the module.

    Written under the settings path so it applies to every invocation reading that
    dir; the loop denies the same tools on its own command line, which covers the
    round it launches itself.
    """
    issued = _installed_commands(monkeypatch)
    writes = [c for c in issued if "settings.json" in c]
    assert writes, (
        "install issues no command writing settings.json, so the deny exists only "
        f"in the module: {issued}"
    )
    # The redirect and the path together: a later chmod naming the right file says
    # nothing about where the content went.
    target = f"> {harbor_claude._CLAUDE_HOME}/settings.json"
    write = [c for c in writes if target in c]
    assert write, f"nothing writes to {target!r}: {writes}"
    for tool in sorted(DENIED_TOOLS):
        assert tool in write[0], f"{tool} is not in the installed settings: {write[0]}"


CODEX_LOOP = Path(harbor_agent._LOOP_SCRIPT)
CODEX_CONFIG = "{_CODEX_HOME}/config.toml"


def _codex_invocations() -> list[str]:
    """Each ``codex exec`` in the loop, as its full backslash-continued command."""
    text = CODEX_LOOP.read_text(encoding="utf-8")
    out, lines = [], text.splitlines()
    for i, line in enumerate(lines):
        if "codex exec" not in line or line.lstrip().startswith("#"):
            continue
        block = [line]
        while block[-1].rstrip().endswith("\\"):
            block.append(lines[i + len(block)])
        out.append("\n".join(block))
    return out


def test_the_codex_loop_pins_the_web_setting_on_every_invocation():
    """Both branches: a first round and a resumed one are the same measurement.

    Pinned on the command line because it overrides the config file, which the
    agent shares a container with.
    """
    calls = _codex_invocations()
    runs = [c for c in calls if "$round_budget" in c]
    assert len(runs) == 2, f"expected the two round invocations, found {len(runs)}"
    for call in runs:
        assert "WEB_SEARCH_FLAG" in call, call


def test_the_codex_config_is_readable_but_not_writable_by_the_agent():
    """The config is installed 0644 at both sites that write it."""
    text = harbor_agent.__file__ and Path(harbor_agent.__file__).read_text(
        encoding="utf-8")
    assert f"chmod 0666 {CODEX_CONFIG}" not in text, (
        "config.toml is installed world-writable; the agent uid can rewrite it"
    )
    # Two sites -- install() and run() -- and a deleted chmod would fail here.
    assert text.count(f"chmod 0644 {CODEX_CONFIG}") == 2, text.count(
        f"chmod 0644 {CODEX_CONFIG}")


def test_install_refuses_a_client_that_does_not_validate_the_web_setting(monkeypatch):
    """The pin names a key, and an unrecognised key is accepted in silence.

    Driven through the real ``install`` so the probe's verdict is what decides,
    not the presence of the code that issues it.
    """
    monkeypatch.setattr(harbor_agent, "EnvironmentPaths",
                        types.SimpleNamespace(agent_dir=Path("/logs/agent")))

    async def drive(reply: str) -> None:
        agent = object.__new__(harbor_agent.SrbCodex)
        agent._codex_binary_path = lambda: None
        agent._version = harbor_agent.PINNED_VERSION
        agent.parse_version = lambda text: harbor_agent.PINNED_VERSION
        agent.exec_as_root = lambda environment, command: asyncio.sleep(0)
        agent.logs_dir = Path("/tmp")
        agent._render_config = lambda: ""
        agent._get_env = lambda name: None

        class Env:
            async def exec(self, command):
                out = (harbor_agent.PINNED_VERSION if "--version" in command
                       else reply)
                return types.SimpleNamespace(return_code=0, stdout=out, stderr="")

            async def upload_file(self, src, dst):
                pass

        await agent.install(Env())

    # A client that validates the key: the probe's own error is the proof.
    asyncio.run(drive("Error loading config.toml: unknown variant `__srb_probe__`"))

    with pytest.raises(RuntimeError, match="does not validate web_search"):
        asyncio.run(drive("ok"))


def test_the_pin_the_probe_and_the_config_name_one_setting():
    """A probe on a different key would pass while the pin set nothing.

    Three places name it -- the rendered config, the loop's flag and install's
    probe -- and the probe is only evidence about the key actually pinned.
    """
    import re

    source = Path(harbor_agent.__file__).read_text(encoding="utf-8")
    loop = CODEX_LOOP.read_text(encoding="utf-8")
    keys = {
        "config": re.findall(r"^(\w+) = \"disabled\"", source, re.M),
        "flag": re.findall(r"WEB_SEARCH_FLAG=\(-c (\w+)=", loop),
        "probe": re.findall(r"-c (\w+)=__srb_probe__", source),
    }
    for where, found in keys.items():
        assert len(found) == 1, f"{where}: expected one setting name, got {found}"
    assert len({v[0] for v in keys.values()}) == 1, keys
