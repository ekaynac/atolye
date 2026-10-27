"""mini-swe-agent as the alternative worker (stage-2 test T1: OpenHands vs mini-swe-agent on the same items).

Differences from the OpenHands path, by design of mini-swe-agent:
- the agent loop runs on the host; only its bash commands run in the container, which has no network at all;
- so the model is called from the host, straight to the gateway with the job's virtual key (the key never enters
  the container).
The agent uses mini-swe-agent's own SWE-bench config (tool calling, step limit), with the task text as the issue.
"""

from __future__ import annotations

import os
import threading
from importlib.resources import files
from pathlib import Path

# mini-swe-agent loads a user-wide .env at import time; give it an empty directory of our own instead, so another
# session's settings or keys never reach a measured run.
_CONFIG_DIR = Path(__file__).resolve().parents[2] / "runs" / "mini-config"
_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = str(_CONFIG_DIR)

import yaml  # noqa: E402
from minisweagent.agents.default import DefaultAgent  # noqa: E402
from minisweagent.environments.docker import DockerEnvironment  # noqa: E402
from minisweagent.models.litellm_model import LitellmModel  # noqa: E402

WORKDIR = "/testbed"  # the path mini-swe-agent's SWE-bench prompt names
ITEM_WORKDIR = "/workspace"  # where the item's paths point (P-A runs there); the same checkout is mounted at both


def _config() -> dict:
    return yaml.safe_load((files("minisweagent.config") / "benchmarks" / "swebench.yaml").read_text())


def converse(prompt: str, image: str, project: Path, item_env: dict[str, str], model: str, api_base: str, key: str,
             max_steps: int, timeout_s: float, record: dict, trajectory: Path) -> None:
    cfg = _config()
    env_cfg = {k: v for k, v in cfg["environment"].items() if k not in ("environment_class", "cwd")}
    env_cfg["env"] = {**{k: v for k, v in env_cfg.get("env", {}).items() if k != "BASH_ENV"},  # no conda here
                      **item_env}
    env = DockerEnvironment(
        image=image, cwd=WORKDIR, executable="podman", **env_cfg,
        run_args=["--rm", "--network", "none", "--userns=keep-id", f"--user={os.getuid()}:{os.getgid()}",
                  "--security-opt", "no-new-privileges", "--pids-limit", "1024", "--memory", "8g",
                  "--entrypoint", "/usr/bin/env",
                  "-v", f"{project}:{WORKDIR}:Z", "-v", f"{project}:{ITEM_WORKDIR}:z"],
    )
    model_cfg = {**cfg["model"], "model_name": f"openai/{model}", "cost_tracking": "ignore_errors"}
    model_cfg["model_kwargs"] = {**model_cfg.get("model_kwargs", {}), "api_base": api_base, "api_key": key}
    agent_cfg = {**cfg["agent"], "step_limit": max_steps, "cost_limit": 0, "output_path": trajectory}
    agent = DefaultAgent(LitellmModel(**model_cfg), env, **agent_cfg)
    result: dict = {}
    done = threading.Event()

    def _run() -> None:
        try:
            result.update(agent.run(task=prompt))
        except Exception as exc:  # recorded as the stop cause, like the OpenHands path
            result["exit_status"] = f"error: {type(exc).__name__}: {exc}"[:300]
        finally:
            done.set()

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    if not done.wait(timeout_s):
        agent.config.step_limit = 1  # the next query raises LimitsExceeded and ends the run
        env.cleanup()  # commands now fail fast
        done.wait(120)
        result.setdefault("exit_status", "timeout")
    env.cleanup()
    record.update(stop=str(result.get("exit_status", "unknown")), llm_calls=agent.n_calls,
                  last_agent_event="submitted" if result.get("exit_status") == "Submitted" else "none")
