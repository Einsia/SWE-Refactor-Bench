"""The SWERefactorBench agent adapter for Harbor — codex-cli 0.146.0, offline, resumable.

This module is imported only by Harbor's custom-loader (`harbor run -a
swerefactor.harbor_agent:SrbCodex`), which requires the repository's ``infra/`` on
``PYTHONPATH`` so ``swerefactor`` resolves. It is a submodule of the packaging
root on purpose but imports Harbor only here, so ``python3 -m swerefactor`` under
an older interpreter never drags Harbor in.

Plain ``harbor run -a codex`` distributes and runs the harness wrong for this
benchmark in four ways, and each is closed here:

  * it ``npm install``s a codex whose version is pinned by nothing at run time,
    and fails outright under ``network_mode = no-network``;
  * it runs a *single* ``codex exec``: a gateway capacity error or a dropped
    stream ends the whole trial, and ``--max-retries`` restarts it from a clean
    image, discarding hours of work;
  * it applies no egress allowlist of its own, so the agent reaches the model
    gateway only if the driver remembers ``--allow-agent-host``;
  * it knows nothing about the ``SRB_TASK_COMPLETE`` completion signal the task
    instructions use to distinguish "the model is done" from "a turn happened
    to stop here".

The approach is not to reimplement the harness but to drive the proven one:
the codex-layer round loop (``harness/srb-agent-loop.sh``) does up to 400
rounds of ``codex exec``, resuming the same codex session after each dropped or
truncated round, rotating the thread only when it fills the model's context
window.  We upload the pinned static codex binary and a rendered config into
the container (no package manager, no network during install), assert the
binary is the pinned version, open egress to exactly the model gateway for the
agent phase, run the loop, and mirror its logs and the codex session
recordings back for Harbor to pick up as the run's evidence.

Usage (from the repository root):

    PYTHONPATH=infra harbor run -p tasks/<task-id> \
        -a swerefactor.harbor_agent:SrbCodex \
        -m gpt-5.6-sol -ak version=0.146.0 -ak reasoning_effort=<effort>

On a kernel without the cgroup CPU controller (NanoCPUs is refused and the
environment container will not start), tell Harbor not to apply resource limits:

    ... harbor run ... --cpus ignore --memory ignore

Credentials come from the environment: ``OPENAI_API_KEY`` (or
``--ae OPENAI_API_KEY=...``).  Requests go to the public API by default, and
``OPENAI_BASE_URL`` redirects them the way the grader's own driver reads it.  A
private gateway is named by ``SRB_GATEWAY`` (host or host:port, plain HTTP) or
by ``SRB_GATEWAY_URL`` for a full base URL; either one also becomes the single
host the egress allowlist opens.  The pinned static codex binary is located by
``SRB_CODEX_BINARY`` unless the image carries codex 0.146.0 already, in which
case install only verifies it.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from harbor.agents.installed.base import CliFlag
from harbor.agents.installed.codex import Codex
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.task.config import NetworkMode, NetworkPolicy
from harbor.models.trial.paths import EnvironmentPaths
from harbor.utils.trajectory_utils import format_trajectory_json

#: The one harness version the whole suite is measured with (task [agent].harness).
PINNED_VERSION = "0.146.0"

_HARNESS_DIR = Path(__file__).resolve().parent / "harness"
_LOOP_SCRIPT = _HARNESS_DIR / "srb-agent-loop.sh"

# codex home inside the agent container. The loop writes its session recordings
# under CODEX_HOME/sessions (session_meta + token_count per round), and those are
# the evidence Harbor records.
_CODEX_HOME = "/opt/codex-home"
# container-local log scratch (events-N.jsonl live here; mirrored to the agent
# log dir Harbor downloads).
_LOOP_LOG_DIR = "/opt/srb-agent-logs"

#: The codex config the loop drives.  model / effort / gateway URL are
#: rendered per run; everything else is the proven pin (offline, no approvals,
#: Responses API to the sub2api gateway, generous stream retries).
_CONFIG_TEMPLATE = '''\
# SWERefactorBench agent harness: pinned codex, driven as the test model.
model = "{model}"
model_provider = "sub2api"
model_reasoning_effort = "{effort}"
model_reasoning_summary = "auto"
service_tier = "priority"
preferred_auth_method = "apikey"

# The container is the sandbox; codex's own landlock/seccomp would only add a
# second boundary inside a boundary over Harbor's egress allowlist.
approval_policy = "never"
sandbox_mode = "danger-full-access"

# The task is solved from the repository alone.
web_search = "disabled"

[mcp_servers]

[model_providers.sub2api]
name = "sub2api"
base_url = "{base_url}"
wire_api = "responses"
env_key = "OPENAI_API_KEY"
request_max_retries = 8
stream_max_retries = 10
stream_idle_timeout_ms = 600000
'''


class SrbCodex(Codex):
    """codex-cli driver that reproduces the benchmark's codex layer inside Harbor."""

    PINNED_VERSION = PINNED_VERSION

    # We only need `reasoning_effort` captured as a value (it feeds the rendered
    # config, not a codex CLI flag, because the loop, not Harbor, invokes codex).
    # Default to "ultra", the benchmark's proven codex-layer default.
    CLI_FLAGS = [
        CliFlag(
            "reasoning_effort",
            cli="-c",
            type="str",
            default="ultra",
            format="-c model_reasoning_effort={value}",
        ),
    ]

    def __init__(self, *args, **kwargs):
        self._codex_binary_override = kwargs.pop("codex_binary", None)
        self._deadline_override = kwargs.pop("deadline_sec", None)
        self._gateway_override = kwargs.pop("gateway", None)
        super().__init__(*args, **kwargs)

    # --- configuration helpers ------------------------------------------------

    @staticmethod
    def name() -> str:
        return "codex"

    def _gateway_spec(self) -> str:
        """A private gateway's ``host[:port]``, or empty when there is none.

        Empty is the default on purpose.  This used to fall back to a fixed
        address on the authoring host's own VPC, which is unreachable from
        anywhere else -- so the default cost an external run a full agent
        deadline spent on connections that could never open, and published the
        result as the model's.  The public API is the honest default; a private
        gateway is a thing an operator opts into by naming it.
        """
        return (
            self._gateway_override
            or self._get_env("SRB_GATEWAY")
            or ""
        ).strip()

    def _gateway_base_url(self) -> str:
        """Where codex sends requests, most specific setting first.

        ``OPENAI_BASE_URL`` is last before the public API because the grader's
        own driver reads that variable (``models.py``), so one export points the
        agent and the verifier at the same endpoint.
        """
        url = self._get_env("SRB_GATEWAY_URL")
        if url:
            return url.rstrip("/")
        spec = self._gateway_spec()
        if spec:
            return f"http://{spec}/v1"
        return (self._get_env("OPENAI_BASE_URL")
                or "https://api.openai.com/v1").rstrip("/")

    def _gateway_host(self) -> str:
        """The one host the egress allowlist opens.

        Derived from the base URL rather than from ``SRB_GATEWAY`` alone: the
        allowlist has to name whatever the client will actually dial, and three
        settings can decide that.
        """
        spec = self._gateway_base_url()
        spec = spec.split("://", 1)[-1]     # strip scheme if a URL was given
        host = spec.split("/", 1)[0]         # strip any path
        host = host.rsplit(":", 1)[0] if ":" in host else host  # strip port
        return host

    #: How much of the task's declared agent budget is left to the loop for
    #: stopping: writing stop-reason.txt, mirroring it, and letting run() harvest
    #: the codex sessions.  Only the write has to fit -- 15 minutes is slack, and
    #: it is slack taken from a budget of hours.
    _WALL_HEADROOM_SEC = 900

    def _declared_wall_sec(self) -> float | None:
        """The task's own ``[agent] timeout_sec``, read from the trial's config.

        Harbor tells an agent its model, its kwargs and its environment, but not
        the deadline it will be killed at: ``AgentContext`` carries token counts
        and cost, and ``run()`` is handed no task.  The trial directory does know
        -- ``config.json`` records ``task.path`` -- and ``logs_dir`` is inside the
        trial, so the wall is two reads away.

        Both are best-effort on purpose.  A missing or unreadable config must not
        fail a launch; it falls back to the ceiling below, which is exactly the
        behaviour every run before this had.
        """
        try:
            cfg = json.loads(
                (Path(self.logs_dir).parent / "config.json").read_text(encoding="utf-8")
            )
            task_path = Path(str((cfg.get("task") or {}).get("path") or ""))
            if not task_path.parts:
                return None
            # `task.path` is written relative to Harbor's cwd (the repo root).
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
        # Derived from the task's declared wall, because the loop is the only thing
        # that can write a stop_reason and it has to be alive to write one.
        #
        # The comment that used to sit here said Harbor's wait_for(task
        # [agent].timeout_sec) "is the real wall-clock enforcer, so this only needs
        # to be comfortably above it".  That has the dependency backwards.  Harbor's
        # wall does end the phase, but it ends it by raising AgentTimeoutError
        # through the exec -- the loop dies mid-round, having written nothing, and
        # `agent/stop-reason.txt` never appears.  A phase that ended without any
        # stop-reason written at all is indistinguishable from one that ran out of
        # turns, so a run cut by the wall published no measurement, however much
        # work it had done.
        #
        # It cost two: build01/max (declared wall 21600s) and pf02/max (36000s),
        # the latter killed at round 19 with its own counter reporting 111472s
        # still left.  A ceiling of 144000 sits above *every* task in the suite --
        # the largest declared wall is lang01/lang04 at 108000 -- so the loop could
        # never reach its own deadline first, and the intended design never ran.
        #
        # Under the wall instead, the loop stops itself at the top of a round,
        # writes `deadline`, and the phase ends the way the other 100 ended.
        wall = self._declared_wall_sec()
        if wall and wall > self._WALL_HEADROOM_SEC * 2:
            return int(wall - self._WALL_HEADROOM_SEC)
        # No config to read (or a wall so short the headroom would dominate it):
        # keep the old ceiling, so this is never the reason a launch behaves worse
        # than it did before.
        return 144000

    def _codex_binary_path(self) -> Path | None:
        candidates = []
        if self._codex_binary_override:
            candidates.append(Path(self._codex_binary_override))
        env = self._get_env("SRB_CODEX_BINARY")
        if env:
            candidates.append(Path(env))
        candidates.append(_HARNESS_DIR / "codex")
        candidates += [
            Path.home() / "srb-harness/codex-layer/codex",
            Path.home() / "_keep/codex-layer-0.146.0/codex",
        ]
        for c in candidates:
            if c and c.is_file():
                return c
        return None

    def _render_config(self) -> str:
        return _CONFIG_TEMPLATE.format(
            model=self.model_name or "",
            effort=self._resolved_flags.get("reasoning_effort", "ultra"),
            base_url=self._gateway_base_url(),
        )

    # --- the two phases --------------------------------------------------------

    async def install(self, environment: BaseEnvironment) -> None:
        """Place the pinned harness into the container. No network is used."""
        pinned = self._version or PINNED_VERSION
        # 0777 on codex-home so a non-root [agent] user (7 tasks drop to uid 2000)
        # can create CODEX_HOME/sessions -- the resume loop writes there every round.
        await self.exec_as_root(
            environment,
            "install -d -m 0777 {home} /opt/srb-agent; "
            "install -d -m 0777 {logs}".format(home=_CODEX_HOME, logs=_LOOP_LOG_DIR),
        )
        await self.exec_as_root(
            environment, f"install -d -m 0777 {EnvironmentPaths.agent_dir.as_posix()}"
        )

        # The codex binary: if the environment image already carries
        # codex-cli at the pinned version, install only verifies it (the doc
        # "works if baked into the image"); otherwise upload the static binary
        # from SRB_CODEX_BINARY. Either way the harness pin is asserted below.
        probe = await environment.exec(command="/usr/local/bin/codex --version")
        existing = (
            self.parse_version(probe.stdout or "")
            if probe.return_code == 0
            else None
        )
        if existing != pinned:
            binary = self._codex_binary_path()
            if binary is None:
                raise FileNotFoundError(
                    "No pinned static codex 0.146.0 found. Set SRB_CODEX_BINARY, "
                    "or place it at " + str(_HARNESS_DIR / "codex")
                )
            await environment.upload_file(str(binary), "/usr/local/bin/codex")
            await self.exec_as_root(environment, "chmod 0755 /usr/local/bin/codex")

        # The rendered config and the proven round loop.
        with tempfile.NamedTemporaryFile(
            "w", suffix=".toml", delete=False, dir=str(self.logs_dir)
        ) as cfg:
            cfg.write(self._render_config())
            cfg_path = cfg.name
        try:
            await environment.upload_file(cfg_path, f"{_CODEX_HOME}/config.toml")
            await environment.upload_file(
                str(_LOOP_SCRIPT), "/opt/srb-agent/srb-agent-loop.sh"
            )
        finally:
            Path(cfg_path).unlink(missing_ok=True)
        await self.exec_as_root(
            environment,
            f"chmod 0644 {_CODEX_HOME}/config.toml; "
            "chmod 0755 /opt/srb-agent/srb-agent-loop.sh; "
            f"install -m 0777 -d {_CODEX_HOME}/sessions; "
            f"install -m 0777 -d {EnvironmentPaths.agent_dir.as_posix()}; "
            f"install -m 0777 -d {_LOOP_LOG_DIR}",
        )

        # Fail closed on the wrong harness: the score is a model-and-harness pair.
        result = await environment.exec(command="/usr/local/bin/codex --version")
        self._version = self.parse_version(result.stdout or "")
        if self._version != pinned:
            raise RuntimeError(
                f"codex harness is {self._version!r}, benchmark pins {pinned!r}"
            )

        # The web setting is pinned by name, and an unrecognised name is accepted
        # in silence.  Reject a deliberately invalid value to prove the client
        # still validates that key, rather than launch a run whose offline claim
        # rests on a setting nothing reads.
        probe = await environment.exec(
            command="/usr/local/bin/codex exec -c web_search=__srb_probe__ x"
        )
        if "unknown variant" not in ((probe.stdout or "") + (probe.stderr or "")):
            raise RuntimeError(
                "codex does not validate web_search; the pinned value proves nothing"
            )

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        if not self.model_name:
            raise ValueError("Model name is required")
        # Fail fast on a missing key rather than let the loop spend the deadline
        # backing off a 401 it cannot recover from.
        if not self._get_env("OPENAI_API_KEY"):
            raise ValueError(
                "OPENAI_API_KEY is not set (use --ae OPENAI_API_KEY=... or export it)"
            )
        # Apply any configured prompt template (the base Codex.run decorator does
        # this; an override has to do it by hand).
        instruction = self.render_instruction(instruction)

        # Re-render the config against this run's model/effort (model is known by
        # now; install rendered with whatever was passed, so redo it to be exact).
        with tempfile.NamedTemporaryFile(
            "w", suffix=".toml", delete=False, dir=str(self.logs_dir)
        ) as cfg:
            cfg.write(self._render_config())
            cfg_path = cfg.name
        try:
            await environment.upload_file(cfg_path, f"{_CODEX_HOME}/config.toml")
            # upload_file lands as root mode 0600; a non-root agent user cannot
            # read its config. install() set this too -- redo it on every rewrite.
            await self.exec_as_root(
                environment, f"chmod 0644 {_CODEX_HOME}/config.toml"
            )
        finally:
            Path(cfg_path).unlink(missing_ok=True)

        # Hand the loop the task instruction as a file (its rounds read it).
        instr_tmp = Path(self.logs_dir) / ".srb-instruction.md"
        instr_tmp.write_text(instruction, encoding="utf-8")
        try:
            await environment.upload_file(str(instr_tmp), "/opt/srb-agent/instruction.md")
        finally:
            instr_tmp.unlink(missing_ok=True)

        # Open egress to exactly the model gateway for this phase. On a no-network
        # task Harbor's egress-control sidecar is enabled, so this switches the
        # agent container to gateway-only; on a PUBLIC-network task the gateway is
        # already reachable and egress control is off, so the switch is skipped.
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
            except Exception as exc:  # fail closed: no gateway = no model
                raise RuntimeError(
                    f"cannot open egress allowlist to {self._gateway_host()}: {exc}"
                ) from exc

        deadline = self._deadline_sec()
        # PATH is deliberately not set: the environment image's PATH carries the
        # task's own toolchain (/usr/local/cargo/bin, /opt/go/bin, /opt/venv/bin,
        # ...) plus /usr/local/bin where codex lives. Forcing a minimal PATH would
        # strip those, so the loop inherits the container's default.
        env = {
            "CODEX_HOME": _CODEX_HOME,
            "OPENAI_API_KEY": self._get_env("OPENAI_API_KEY") or "",
            "SRB_INSTRUCTION": "/opt/srb-agent/instruction.md",
            "SRB_AGENT_LOG_DIR": _LOOP_LOG_DIR,
            "SRB_AGENT_MIRROR_DIR": EnvironmentPaths.agent_dir.as_posix(),
            "SRB_AGENT_DEADLINE_SEC": str(deadline),
            "SRB_AGENT_MAX_ROUNDS": "400",
            "TERM": "dumb",
        }
        # The loop exits on its own (completion sentinel / idle / context / its
        # deadline). Harbor's wait_for(task timeout) is the outer backstop; give
        # exec a headroom so it is the loop, not the timeout, that ends the round.
        #
        # With the deadline derived from the declared wall (see _deadline_sec) the
        # three limits are now ordered, which is the whole point: the loop stops at
        # wall-900, this exec would give up at wall-300, and Harbor's wait_for fires
        # at the wall.  Whichever of the outer two ever fires, it fires after the
        # loop has already written its stop_reason.  Before, all three were the same
        # limit in the wrong order -- 144000 sat above every task's wall, so Harbor
        # always won and nothing a run it cut could say about itself was recorded.
        loop_timeout = deadline + 600
        try:
            await self.exec_as_agent(
                environment,
                "bash /opt/srb-agent/srb-agent-loop.sh",
                cwd="/workspace/repo",
                env=env,
                timeout_sec=loop_timeout,
            )
        except Exception as exc:  # a non-zero/timeout loop is not a Harbor error
            self.logger.debug("srb-agent loop returned: %s", exc)
        finally:
            # Harvest the codex session recordings into the agent log dir Harbor
            # downloads, then restore the network policy we found -- the trial's
            # own phase context only restores a switch it tracked, and this one
            # was ours, so it is ours to undo.
            try:
                await self.exec_as_agent(
                    environment,
                    f"cp -r {_CODEX_HOME}/sessions "
                    f"{EnvironmentPaths.agent_dir.as_posix()}/sessions 2>/dev/null; true",
                    env={},
                )
            except Exception:
                pass
            if not prior_is_public and prior_policy is not None:
                try:
                    await environment.set_network_policy(prior_policy)
                except Exception:
                    pass

    # --- evidence --------------------------------------------------------------

    @staticmethod
    def _is_fork_recording(path: Path) -> bool:
        """A subagent fork's first line is a session_meta with forked_from_id.

        The round loop resumes only the root thread; forks are subagent calls it
        spawned, and the base codex adapter ignores them too. Exclude them so the
        trajectory and tokens describe the root thread, matching that behavior.
        """
        try:
            with path.open("rb") as fh:
                first = b""
                for raw in fh:
                    first = raw
                    break
            first = first.decode("utf-8", errors="replace").strip()
            return bool(first) and '"forked_from_id"' in first
        except OSError:
            return False

    @staticmethod
    def _gather_session_files(session_root: Path) -> list[Path]:
        """Root-thread codex recordings under session_root, chronological.

        Skips subagent forks (forked_from_id), the loop's retired thread files
        it kept for the record (``*.retired-N`` -- still root threads, so kept),
        and our own ``_merged`` output (so a re-run does not feed on itself).
        Sort is by name: rollout files are ``rollout-<UTC-ts>-<uuid>.jsonl``,
        which sorts chronologically without depending on mtimes that a plain
        ``cp -r`` (no ``-p``) rewrites.
        """
        if not session_root.is_dir():
            return []
        out: list[Path] = []
        for p in session_root.rglob("*"):
            if not p.is_file():
                continue
            if "_merged" in p.parts:
                continue
            if not (p.name.endswith(".jsonl") or ".retired-" in p.name):
                continue
            if SrbCodex._is_fork_recording(p):
                continue
            out.append(p)
        out.sort(key=lambda p: p.name)
        return out

    def _merge_session_recordings(self, files: list[Path]) -> Path | None:
        """Concatenate the root-thread recordings into one pseudo-session file,
        so the base converter reads the whole run end-to-end.

        The loop resumes the same thread across rounds (one growing rollout
        file), but thread rotations and UTC day-boundaries make several. The
        base ``_convert_events_to_trajectory`` globs ``*.jsonl`` unsorted and
        reads only ``session_files[0]`` -- with resume rounds that is one
        arbitrary round. Merging every root file preserves the run.
        """
        if not files:
            return None
        session_root = files[0].parent.parent  # .../sessions/<YYYY>/<MM>/<DD>
        # walk up to the sessions root (the dir named "sessions")
        for parent in files[0].parents:
            if parent.name == "sessions":
                session_root = parent
                break
        merged_dir = session_root / "_merged"
        merged_dir.mkdir(parents=True, exist_ok=True)
        for old in merged_dir.iterdir():
            if old.is_file():
                old.unlink()
        merged = merged_dir / "session.jsonl"
        with merged.open("w") as out:
            for f in files:
                for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.strip():
                        out.write(line + "\n")
        return merged_dir

    @staticmethod
    def _thread_final_usage(files: list[Path]) -> dict[str, int]:
        """Sum each root thread's final cumulative token usage.

        ``token_count.total_token_usage`` is cumulative *within one thread*;
        the last such event in a file is that thread's total. A run that rotated
        threads has several, and the base converter would report only the last
        thread's total -- an undercount. Summing each thread's final is the run
        total.
        """
        tot = {"input": 0, "output": 0, "cached": 0, "reasoning": 0}
        for f in files:
            last: dict | None = None
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") != "event_msg":
                    continue
                payload = ev.get("payload")
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                # `or {}` and not a get() default: the default only applies when the key
                # is ABSENT, and codex emits `"info": null` on a rate-limits-only
                # token_count (seen once at 16s into a run, against 1405 well-formed
                # events in the same rollout).  The key is present and null, so the
                # default never fires and `.get` raised -- destroying two completed
                # submissions before this was found.
                usage = (payload.get("info") or {}).get("total_token_usage")
                if isinstance(usage, dict):
                    last = usage
            if last:
                tot["input"] += int(last.get("input_tokens") or 0)
                tot["output"] += int(last.get("output_tokens") or 0)
                tot["cached"] += int(last.get("cached_input_tokens") or 0)
                tot["reasoning"] += int(last.get("reasoning_output_tokens") or 0)
        return tot

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Convert codex session recordings into an ATIF trajectory + metrics.

        The base Codex implementation assumes a single recorded session and
        breaks on resume rounds (it reads one arbitrary file, and a UTC
        day-boundary raises "Expected exactly 1 session"). Merge the root
        threads first, reuse the base conversion for the step list, and sum
        per-thread token totals rather than trust the merged stream's last
        token_count (which undercounts a run that rotated threads).
        """
        session_root = self.logs_dir / "sessions"
        files: list[Path] = []
        merged_dir: Path | None = None
        try:
            files = self._gather_session_files(session_root)
            if files:
                merged_dir = self._merge_session_recordings(files)
        except Exception:
            self.logger.exception("failed to merge codex sessions; falling back")

        if merged_dir is None:
            return  # no sessions at all; nothing to convert

        try:
            trajectory = self._convert_events_to_trajectory(merged_dir)
        except Exception:
            self.logger.exception("Failed to convert Codex events to trajectory")
            return
        if not trajectory:
            return

        trajectory_path = self.logs_dir / "trajectory.json"
        try:
            with open(trajectory_path, "w") as handle:
                handle.write(format_trajectory_json(trajectory.to_json_dict()))
        except OSError as exc:
            self.logger.debug(f"Failed to write trajectory file {trajectory_path}: {exc}")

        # Sum each thread's final cumulative usage (correct under rotation); the
        # base's last-token_count only holds for a single-thread run.
        #
        # Guarded like the merge and the conversion above, and for the same reason: this
        # runs inside _sync_agent_output, BEFORE artifact collection, so anything raising
        # here loses the agent's finished repo -- and with environment.delete=True the
        # container is then removed, making the loss unrecoverable.  Usage is metadata;
        # it must never outrank the submission it describes.  A run with unknown cost is
        # a gradeable run, so on failure keep the trajectory and leave the counters at 0.
        try:
            usage = self._thread_final_usage(files)
        except Exception:
            self.logger.exception("failed to sum token usage; leaving counters unset")
            return
        context.n_input_tokens = usage["input"]
        context.n_output_tokens = usage["output"]
        context.n_cache_tokens = usage["cached"]
        context.cost_usd = self._compute_cost_from_pricing(
            prompt_tokens=usage["input"],
            completion_tokens=usage["output"],
            cached_tokens=usage["cached"],
        )

