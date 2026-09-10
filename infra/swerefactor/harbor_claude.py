"""The SWERefactorBench agent adapter for Harbor — Claude Code 2.1.220, offline and resumable.

Companion to :mod:`swerefactor.harbor_agent` (the codex adapter). Where that one
pins the codex-cli binary, this one pins the Claude Code 2.1.220 native binary
(``bin/claude.exe`` — a dynamically-linked Linux ELF, not the JS wrapper), so
it runs in any environment image that has glibc, with no node and no network
during install.

Plain ``harbor run -a claude-code`` installs Claude Code over the network
(npm or the bootstrap script) and runs a single ``claude --print`` turn, so a
gateway capacity error or a dropped stream ends the trial. This adapter uploads
the pinned static binary, opens egress to exactly the model gateway for the
agent phase, and drives ``harness/srb-claude-loop.sh`` — a round loop that
resumes the same Claude session (``--continue``) after each round and
recognises the ``SRB_TASK_COMPLETE`` completion signal, mirroring the codex
adapter's contract.

Usage (from the repository root):

    PYTHONPATH=infra harbor run -p tasks/<task-id> \
        -a swerefactor.harbor_claude:SrbClaudeCode \
        -m claude-opus-5 -ak version=2.1.220 -ak reasoning_effort=<effort>

On a kernel without the cgroup CPU controller: ``... --cpus ignore --memory ignore``.

Credentials come from the environment: ``ANTHROPIC_API_KEY`` (and optionally
``ANTHROPIC_BASE_URL``). The pinned binary is found via ``SRB_CLAUDE_BINARY``,
or beside this module at ``harness/claude``.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from harbor.agents.installed.claude_code import ClaudeCode
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.task.config import NetworkMode, NetworkPolicy
from harbor.models.trial.paths import EnvironmentPaths

#: The one Claude Code version the benchmark's claude/deepseek/glm family is measured with.
PINNED_VERSION = "2.1.220"

_HARNESS_DIR = Path(__file__).resolve().parent / "harness"
_LOOP_SCRIPT = _HARNESS_DIR / "srb-claude-loop.sh"

# Claude Code writes its session recordings under ~/.claude by default; the loop
# points it here so the harvest + the base trajectory converter find one tree.
_CLAUDE_HOME = "/opt/claude-home"
_LOOP_LOG_DIR = "/opt/srb-agent-logs"

#: The task is solved from the repository alone.  These two are denied by name, so
#: they are never offered.
_DENIED_TOOLS = ("WebSearch", "WebFetch")
_SETTINGS_JSON = json.dumps({"permissions": {"deny": list(_DENIED_TOOLS)}})


class SrbClaudeCode(ClaudeCode):
    """Claude Code 2.1.220 driver that reproduces the benchmark's resumable loop."""

    PINNED_VERSION = PINNED_VERSION

    def __init__(self, *args, **kwargs):
        self._binary_override = kwargs.pop("claude_binary", None)
        self._gateway_override = kwargs.pop("gateway", None)
        self._deadline_override = kwargs.pop("deadline_sec", None)
        super().__init__(*args, **kwargs)

    # --- configuration helpers ------------------------------------------------

    @staticmethod
    def name() -> str:
        return "claude-code"

    def _gateway_host(self) -> str:
        spec = (
            self._gateway_override
            or self._get_env("SRB_ANTHROPIC_GATEWAY")
            or self._get_env("ANTHROPIC_BASE_URL")
            or "https://api.anthropic.com"
        )
        spec = spec.split("://", 1)[-1].split("/", 1)[0]
        return spec.rsplit(":", 1)[0] if ":" in spec else spec

    def _anthropic_base_url(self) -> str:
        # secrets.env sets ANTHROPIC_BASE_URL already; honour it if present.
        return self._get_env("ANTHROPIC_BASE_URL") or ""

    #: How much of the task's declared agent budget is left to the loop for
    #: stopping: writing stop-reason.txt, mirroring it, and letting run() harvest
    #: the Claude session tree.  Matches the codex adapter's figure.
    _WALL_HEADROOM_SEC = 900

    def _declared_wall_sec(self) -> float | None:
        """The task's own ``[agent] timeout_sec``, read from the trial's config.

        Same two reads as :meth:`swerefactor.harbor_agent.SrbCodex._declared_wall_sec`
        — Harbor hands an agent no deadline, but the trial directory records
        ``task.path`` in ``config.json`` and ``logs_dir`` sits inside the trial.
        Best-effort on purpose: an unreadable config falls back to the old ceiling
        rather than failing a launch.
        """
        try:
            cfg = json.loads(
                (Path(self.logs_dir).parent / "config.json").read_text(encoding="utf-8")
            )
            task_path = Path(str((cfg.get("task") or {}).get("path") or ""))
            if not task_path.parts:
                return None
            toml = (task_path / "task.toml").resolve()
            if not toml.is_file():
                return None
            from . import tomlcompat

            val = (tomlcompat.load(toml).get("agent") or {}).get("timeout_sec")
            return float(val) if val else None
        except Exception:
            return None

    def _deadline_sec(self) -> int:
        if self._deadline_override is not None:
            return int(self._deadline_override)
        val = self._get_env("SRB_AGENT_DEADLINE_SEC")
        if val:
            return int(float(val))
        # Derived from the task's declared wall, for the reason spelled out in
        # harbor_agent._deadline_sec: the loop is the only thing that can write a
        # stop_reason, and it has to be alive to write one.  A flat 144000 sits
        # above *every* task's declared wall (the largest is lang01/lang04 at
        # 108000), so Harbor's wait_for always won the race, killed the exec
        # mid-round, and the phase published no stop-reason at all -- which is
        # indistinguishable from a phase that ran out of turns.  Deriving the
        # deadline from the wall lets the loop stop itself and say why.
        wall = self._declared_wall_sec()
        if wall and wall > self._WALL_HEADROOM_SEC * 2:
            return int(wall - self._WALL_HEADROOM_SEC)
        return 144000

    def _binary_path(self) -> Path | None:
        candidates = []
        if self._binary_override:
            candidates.append(Path(self._binary_override))
        env = self._get_env("SRB_CLAUDE_BINARY")
        if env:
            candidates.append(Path(env))
        candidates += [
            _HARNESS_DIR / "claude",
            Path.home() / "srb-harness/claude-layer/prefix/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
        ]
        for c in candidates:
            if c and c.is_file():
                return c
        return None

    # --- the two phases --------------------------------------------------------

    async def install(self, environment: BaseEnvironment) -> None:
        """Place the pinned binary + loop into the container. No network is used."""
        pinned = self._version or PINNED_VERSION
        await self.exec_as_root(
            environment,
            f"install -d -m 0777 {_CLAUDE_HOME} /opt/srb-agent; "
            f"install -d -m 0777 {_LOOP_LOG_DIR}",
        )
        await self.exec_as_root(
            environment, f"install -d -m 0777 {EnvironmentPaths.agent_dir.as_posix()}"
        )

        # If the image already carries Claude Code at the pin, install only
        # verifies it; otherwise upload the static ELF binary.
        probe = await environment.exec(command="claude --version")
        existing = (
            self.parse_version(probe.stdout or "") if probe.return_code == 0 else None
        )
        if existing != pinned:
            binary = self._binary_path()
            if binary is None:
                raise FileNotFoundError(
                    "No pinned Claude Code 2.1.220 binary found. Set "
                    "SRB_CLAUDE_BINARY, or place it at " + str(_HARNESS_DIR / "claude")
                )
            await environment.upload_file(str(binary), "/usr/local/bin/claude")
            await self.exec_as_root(environment, "chmod 0755 /usr/local/bin/claude")

        await environment.upload_file(
            str(_LOOP_SCRIPT), "/opt/srb-agent/srb-claude-loop.sh"
        )
        await self.exec_as_root(
            environment,
            "chmod 0755 /opt/srb-agent/srb-claude-loop.sh; "
            f"install -m 0777 -d {_CLAUDE_HOME}/projects; "
            f"install -m 0777 -d {EnvironmentPaths.agent_dir.as_posix()}; "
            f"install -m 0777 -d {_LOOP_LOG_DIR}",
        )

        # Settings deny covers every invocation reading this config dir, not only
        # the loop's own; the loop denies the same tools on its command line.
        await self.exec_as_root(
            environment,
            f"printf '%s' '{_SETTINGS_JSON}' > {_CLAUDE_HOME}/settings.json; "
            f"chmod 0644 {_CLAUDE_HOME}/settings.json; "
            # Sticky, so the agent uid can write its sessions here but cannot
            # unlink a file it does not own.
            f"chmod 1777 {_CLAUDE_HOME}",
        )

        result = await environment.exec(command="claude --version")
        self._version = self.parse_version(result.stdout or "")
        if self._version != pinned:
            raise RuntimeError(
                f"claude-code harness is {self._version!r}, benchmark pins {pinned!r}"
            )

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        if not self.model_name:
            raise ValueError("Model name is required")
        if not self._get_env("ANTHROPIC_API_KEY"):
            raise ValueError(
                "ANTHROPIC_API_KEY is not set (use --ae ANTHROPIC_API_KEY=... or export it)"
            )
        instruction = self.render_instruction(instruction)

        # Hand the loop the task instruction as a file.
        instr_tmp = Path(self.logs_dir) / ".srb-instruction.md"
        instr_tmp.write_text(instruction, encoding="utf-8")
        try:
            await environment.upload_file(str(instr_tmp), "/opt/srb-agent/instruction.md")
        finally:
            instr_tmp.unlink(missing_ok=True)

        # Open egress to exactly the model gateway (skip if already public).
        prior_policy = getattr(environment, "_network_policy", None)
        prior_is_public = (
            prior_policy is not None
            and prior_policy.network_mode == NetworkMode.PUBLIC
        )
        if not prior_is_public:
            try:
                await environment.set_network_policy(
                    NetworkPolicy(
                        network_mode=NetworkMode.ALLOWLIST,
                        allowed_hosts=[self._gateway_host()],
                    )
                )
            except Exception as exc:
                raise RuntimeError(
                    f"cannot open egress allowlist to {self._gateway_host()}: {exc}"
                ) from exc

        # effort comes through CLI_FLAGS' reasoning_effort descriptor.
        effort = self._resolved_flags.get("reasoning_effort")
        env = {
            "ANTHROPIC_API_KEY": self._get_env("ANTHROPIC_API_KEY") or "",
            "ANTHROPIC_MODEL": self.model_name,
            "CLAUDE_CONFIG_DIR": _CLAUDE_HOME,
            # Suppress statsig/telemetry chatter; under the egress allowlist those
            # hosts are refused anyway, but this stops up to 400 rounds of retries.
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "SRB_INSTRUCTION": "/opt/srb-agent/instruction.md",
            "SRB_AGENT_LOG_DIR": _LOOP_LOG_DIR,
            "SRB_AGENT_MIRROR_DIR": EnvironmentPaths.agent_dir.as_posix(),
            "SRB_AGENT_DEADLINE_SEC": str(self._deadline_sec()),
            "SRB_AGENT_MAX_ROUNDS": "400",
            "SRB_MODEL": self.model_name,
            "TERM": "dumb",
            # The client aborts a live request after this long with no chunk
            # ("API Error: Stream idle timeout - no chunks received"), and its
            # floor is Math.max(env, 300000) -- 300s, raisable only upwards.
            #
            # 300s is too short against this gateway: it forwards thinking_delta
            # only in a final burst, not live (probed 08-08 -- 62 deltas, first
            # at 10.47s of a 10.5s stream), so a request is silent for as long as
            # the model thinks.  At max effort, and across an auto-compaction
            # (measured 242s for 176434 -> 18722 tokens), that silence passes
            # 300s and a *healthy* request is dropped mid-flight.  It cost the
            # lang01/max pilot round 1: rc=1 at 1034s, $1.84, 1.55M cache-read
            # tokens re-established on resume.
            #
            # 600s: over 2x the observed compaction, and low enough that the
            # client's own retry/abort path still finishes inside the loop's
            # 1800s MAX_ROUND_SEC -- so a genuinely wedged request ends as a
            # clean rc=1 the loop resumes with --continue, not a round the loop
            # has to kill and count as a wedge.
            "CLAUDE_STREAM_IDLE_TIMEOUT_MS": "600000",
            "CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS": "600000",
        }
        if effort:
            env["SRB_CLAUDE_EFFORT"] = effort
        if self._anthropic_base_url():
            env["ANTHROPIC_BASE_URL"] = self._anthropic_base_url()
            # Against a custom gateway, pin every model alias Claude Code knows to
            # the benchmark model, so a subagent/background call does not leave for
            # a bundled haiku/sonnet name the gateway would 404.
            for alias in ("ANTHROPIC_DEFAULT_SONNET_MODEL",
                          "ANTHROPIC_DEFAULT_OPUS_MODEL",
                          "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                          "CLAUDE_CODE_SUBAGENT_MODEL"):
                env[alias] = self.model_name
            env["ANTHROPIC_BASE_URL"] = self._anthropic_base_url()

        # Three ordered limits, as in harbor_agent: the loop stops itself at
        # wall-900 (_deadline_sec), this exec gives up at wall-300, and Harbor's
        # wait_for fires at the wall.  Whichever outer one fires, it fires after
        # the loop has already written its stop_reason.
        loop_timeout = self._deadline_sec() + 600
        try:
            await self.exec_as_agent(
                environment,
                "bash /opt/srb-agent/srb-claude-loop.sh",
                cwd="/workspace/repo",
                env=env,
                timeout_sec=loop_timeout,
            )
        except Exception as exc:
            self.logger.debug("srb-claude loop returned: %s", exc)
        finally:
            # Restore the network policy BEFORE harvesting the session tree: a
            # harbor timeout cancels the exec await without killing the in-container
            # loop, so closing egress first stops an orphaned round from keeping the
            # gateway open (and writing to the session file) while we copy it.
            if not prior_is_public and prior_policy is not None:
                try:
                    await environment.set_network_policy(prior_policy)
                except Exception:
                    pass
            # Claude Code stores sessions under CLAUDE_CONFIG_DIR/projects; harvest
            # them so the base trajectory converter (which reads logs_dir/sessions)
            # has one tree. The base _get_session_dir expects sessions/projects/.
            try:
                await self.exec_as_agent(
                    environment,
                    f"rm -rf {EnvironmentPaths.agent_dir.as_posix()}/sessions; "
                    f"mkdir -p {EnvironmentPaths.agent_dir.as_posix()}/sessions; "
                    f"cp -r {_CLAUDE_HOME}/projects "
                    f"{EnvironmentPaths.agent_dir.as_posix()}/sessions/projects 2>/dev/null; "
                    f"cp -r {_CLAUDE_HOME}/*.jsonl "
                    f"{EnvironmentPaths.agent_dir.as_posix()}/sessions/ 2>/dev/null; true",
                    env={},
                )
            except Exception:
                pass

    # populate_context_post_run is inherited from ClaudeCode: it finds
    # logs_dir/sessions/projects/<hash>/<id>.jsonl and converts the stream-json
    # (assistant/result events carry usage) into an ATIF trajectory + metrics.

    def _usage_from_events(self) -> dict | None:
        """Sum the run's token usage + cost over every round's result event.

        Claude Code's ``--print --output-format stream-json`` emits a terminal
        ``{"type":"result", "usage":{...}, "total_cost_usd":...}`` per round, and
        each round is a fresh process: the usage it reports is *that invocation's*,
        not a cumulative session total (verified against the pinned 2.1.220 binary:
        two ``--continue`` rounds of the same session report in=101 then in=102, not
        203). So the run total is the sum over all rounds' result events.

        The loop mirrors each round to ``events-N.jsonl``; sort numerically so
        events-9 sorts before events-40 (a lexicographic sort would not).
        """
        import json
        tot = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "cost": 0.0}
        found = False

        def evnum(p: Path) -> int:
            try:
                return int(p.stem.split("-")[1])
            except (IndexError, ValueError):
                return 0

        for ev in sorted(self.logs_dir.glob("events-*.jsonl"), key=evnum):
            try:
                text = ev.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "result":
                    continue
                found = True
                usage = rec.get("usage") or {}
                tot["input"] += int(usage.get("input_tokens") or 0)
                tot["output"] += int(usage.get("output_tokens") or 0)
                tot["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
                tot["cache_creation"] += int(usage.get("cache_creation_input_tokens") or 0)
                cost = rec.get("total_cost_usd")
                if cost is not None:
                    tot["cost"] += float(cost)
        return tot if found else None

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Backfill tokens/cost from the loop's stream-json result events.

        The inherited converter writes ``trajectory.json`` from the harvested
        session tree, but its only cost source (``claude-code.txt``, which the
        parent tees) is absent here -- the loop writes ``events-N.jsonl`` instead
        -- so it leaves ``cost_usd`` None even on a successful conversion. The
        events-N result events carry per-round usage and ``total_cost_usd`` (summed
        across rounds, since each is per-invocation), so read the run totals there
        and fill whatever the session path left empty.
        """
        try:
            super().populate_context_post_run(context)
        except Exception:
            self.logger.exception("ClaudeCode trajectory conversion failed")
        tot = self._usage_from_events()
        if not tot:
            return
        if not context.n_input_tokens:
            # input = prompt + cache_read + cache_creation, matching the parent's
            # _build_metrics formula (a bare input_tokens would undercount).
            context.n_input_tokens = (
                tot["input"] + tot["cache_read"] + tot["cache_creation"]
            )
        if not context.n_output_tokens:
            context.n_output_tokens = tot["output"]
        if not context.n_cache_tokens:
            context.n_cache_tokens = tot["cache_read"]
        if context.cost_usd is None and tot["cost"]:
            context.cost_usd = tot["cost"]


