"""Run one item headless with OpenHands and append one metrics line.

Usage: atolye-run ITEM.json --out runs.jsonl [--model M] [--base-url URL] [--max-iterations N] [--timeout S]
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from pydantic import SecretStr

from openhands.sdk import LLM, Conversation
from openhands.tools.preset.default import get_default_agent

from atolye import gateway, vllm_metrics
from atolye.podman import ServerSpec, agent_server, remove_tree
from atolye.referee import acceptance_dir, referee

AGENT_IMAGE = "ghcr.io/openhands/agent-server:1.58.2-python"  # matches the pinned SDK; digest goes in the run record
LOCAL_MODEL = "openai/RadixArk/Qwen3.8-27B-NVFP4"
LOCAL_BASE_URL = "http://host.containers.internal:8000/v1"  # the host's vLLM, seen from a rootless container


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def _image_digest(image: str) -> str:
    out = subprocess.run(["podman", "image", "inspect", image, "--format", "{{.Digest}}"],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


# The sandbox gets a working copy with its own .git, which it can rewrite at will. The host never runs git against
# that .git (a crafted core.fsmonitor, hook or filter driver there would run on the host). The host keeps a separate
# bare repository and reads the sandbox's files through it as a plain work tree, with every program-running hook
# disabled.
_HOST_GIT_SAFE = ["-c", "core.bare=false", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                  "-c", "core.untrackedCache=false"]


def _host_git(gitdir: Path, worktree: Path, *args: str) -> str:
    cmd = ["git", f"--git-dir={gitdir}", f"--work-tree={worktree}", *_HOST_GIT_SAFE, *args]
    return subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=worktree).stdout


def _checkout(repo: str, base: str, dest: Path, host_gitdir: Path) -> str:
    subprocess.run(["git", "clone", "--quiet", repo, str(dest)], check=True)
    _git(dest, "checkout", "--quiet", "--detach", base)
    subprocess.run(["git", "clone", "--quiet", "--bare", repo, str(host_gitdir)], check=True)
    return _git(dest, "rev-parse", "HEAD").strip()  # read before the sandbox exists, so it is still trusted


# Interpreter and tool caches are never part of a change; they are excluded from the patch, not counted as edits.
CACHE_EXCLUDES = [":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/.pytest_cache/**",
                  ":(exclude,glob)**/node_modules/**", ":(exclude,glob)**/.mypy_cache/**"]


def _patch_against(base_sha: str, project: Path, host_gitdir: Path) -> str:
    _host_git(host_gitdir, project, "read-tree", base_sha)  # start the index from base, not from the sandbox
    _host_git(host_gitdir, project, "add", "-A", "--", ".", *CACHE_EXCLUDES)
    return _host_git(host_gitdir, project, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv",
                     base_sha)


def _last_agent_event(events) -> str:
    for ev in reversed(list(events)):
        if getattr(ev, "source", None) == "agent":
            action = getattr(ev, "action", None)
            return type(action).__name__ if action is not None else type(ev).__name__
    return "none"


def _converse(item: dict, image: str, project: Path, llm: LLM, network: str | None, max_iterations: int,
              timeout_s: float, record: dict) -> None:
    with agent_server(ServerSpec(image=image, project_dir=project, network=network)) as ws:
        conv = Conversation(agent=get_default_agent(llm=llm, cli_mode=True), workspace=ws,
                            max_iteration_per_run=max_iterations)
        try:
            conv.send_message(item["prompt"])
            try:
                conv.run(timeout=timeout_s)
                record["stop"] = str(conv.state.execution_status)
            except Exception as exc:  # a timeout or server error is a recorded outcome, not a crash
                record["stop"] = f"error: {type(exc).__name__}: {exc}"[:300]
            m = conv.conversation_stats.get_combined_metrics()
            u = m.accumulated_token_usage
            record.update(
                llm_calls=len(m.response_latencies),
                prompt_tokens=u.prompt_tokens if u else 0,
                completion_tokens=u.completion_tokens if u else 0,
                cache_read_tokens=u.cache_read_tokens if u else 0,
                cost_usd_reported=m.accumulated_cost,
                last_agent_event=_last_agent_event(conv.state.events),
            )
        finally:
            conv.close()


def run(item_path: Path, out: Path, model: str | None, direct: bool, max_iterations: int, timeout_s: float,
        worker: str = "openhands") -> dict:
    if direct and worker != "openhands":
        raise ValueError("--direct is a debugging path for the OpenHands worker only")
    item = json.loads(item_path.read_text())
    root = item_path.parent
    if "prompt_file" in item:  # the agent receives only this text (protocol: no other item field reaches any arm)
        item["prompt"] = (root / item["prompt_file"]).read_text()
    if "referee" in item:
        acceptance_dir(item, root)  # fail now, not after a 30-minute agent run
    work = Path(tempfile.mkdtemp(prefix=f"atolye-{item['id']}-"))
    if "fixture_dir" in item:  # a plain directory becomes a one-commit repository
        origin = work / "origin"
        shutil.copytree(root / item["fixture_dir"], origin)
        _git(origin, "init", "-q", "-b", "main")
        _git(origin, "add", "-A")
        _git(origin, "-c", "user.name=atolye", "-c", "user.email=atolye@localhost", "commit", "-qm", "fixture")
        repo, item["base"] = str(origin), "HEAD"
    else:
        repo = item["repo"] if "://" in item["repo"] else str((root / item["repo"]).resolve())
    project, host_gitdir = work / "project", work / "host.git"
    # An item may name a prepared image (agent-server base + the repository's dependencies at its base commit),
    # so the agent and the referee see the same toolchain.
    image = item.get("agent_image", AGENT_IMAGE)
    started = datetime.now(timezone.utc)
    # Unique even for two runs of one item in the same second: usage is summed by this id.
    job = f"{item['id']}-{started.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    record: dict = {
        "arm": "C-openhands" if worker == "openhands" else f"T1-{worker}",
        "worker": worker,
        "item": item["id"],
        "job": job,
        "started_at": started.isoformat(timespec="seconds"),
        "route": "direct" if direct else "gateway",
        "agent_image": image,
        "max_iterations": max_iterations,
    }
    try:
        record["agent_image_digest"] = _image_digest(image)
        record["base"] = base_sha = _checkout(repo, item["base"], project, host_gitdir)
        if direct:  # debugging only: the sandbox reaches vLLM itself and nothing is counted outside the SDK
            record["model"] = model or LOCAL_MODEL
            llm = LLM(usage_id="builder", model=record["model"], base_url=LOCAL_BASE_URL,
                      api_key=SecretStr("local-no-key"), num_retries=2)
            t0 = time.monotonic()
            _converse(item, image, project, llm, None, max_iterations, timeout_s, record)
            record["wall_s"] = round(time.monotonic() - t0, 1)
        else:  # measured runs: per-job key, usage counted at the gateway
            gw_model = model or gateway.DEFAULT_MODEL
            record["model"] = gw_model
            gateway.up()
            before = vllm_metrics.snapshot()
            with gateway.job_key(job, [gw_model], ttl_s=timeout_s + 600) as key:
                t0 = time.monotonic()  # the agent's time only: no gateway start-up or key propagation
                if worker == "mini":  # agent loop on the host, commands in a container with no network
                    from atolye import mini_worker  # optional extra: `uv sync --extra mini`

                    record["trajectory"] = str(out.parent / f"{job}.traj.json")
                    mini_worker.converse(item["prompt"], image, project, gw_model, gateway.HOST_URL, key,
                                         max_iterations, timeout_s, record, Path(record["trajectory"]))
                else:  # OpenHands: the sandbox reaches only the front filter
                    llm = LLM(usage_id="builder", model=f"openai/{gw_model}", base_url=gateway.INTERNAL_URL,
                              api_key=SecretStr(key), num_retries=2)
                    _converse(item, image, project, llm, gateway.INTERNAL_NET, max_iterations, timeout_s, record)
                record["wall_s"] = round(time.monotonic() - t0, 1)
            record.update(gateway.usage(job, started))
            if gw_model == gateway.DEFAULT_MODEL:  # the local vLLM route: read its prefix-cache counters
                record.update(vllm_metrics.delta(before, vllm_metrics.snapshot(), record["gw_input_tokens"]))
        patch = _patch_against(base_sha, project, host_gitdir)
        patch_file = out.parent / f"{job}.patch"
        patch_file.write_text(patch)
        record.update(patch=str(patch_file), patch_lines=patch.count("\n"))
        if "referee" in item:
            record["referee"] = referee(item, root, repo, base_sha, patch_file)
    finally:
        remove_tree(work)
    with out.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return record


def main() -> None:
    p = argparse.ArgumentParser(prog="atolye-run")
    p.add_argument("item", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--model", default=None, help="gateway model name (default: the gateway's local model)")
    p.add_argument("--direct", action="store_true", help="debug only: bypass the gateway")
    p.add_argument("--worker", choices=("openhands", "mini"), default="openhands",
                   help="openhands (arm C) or mini (mini-swe-agent, the T1 comparison worker)")
    p.add_argument("--max-iterations", type=int, default=60)
    p.add_argument("--timeout", type=float, default=1800)
    a = p.parse_args()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    print(json.dumps(run(a.item.resolve(), a.out.resolve(), a.model, a.direct, a.max_iterations, a.timeout,
                         a.worker), indent=2))


if __name__ == "__main__":
    main()
