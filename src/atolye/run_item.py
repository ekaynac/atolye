"""Run one item headless with OpenHands and append one metrics line.

Usage: atolye-run ITEM.json --out runs.jsonl [--model M] [--base-url URL] [--max-iterations N] [--timeout S]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from pydantic import SecretStr

from openhands.sdk import LLM, Conversation
from openhands.tools.preset.default import get_default_agent

from atolye import gateway
from atolye.podman import ServerSpec, agent_server, remove_tree
from atolye.referee import referee

AGENT_IMAGE = "ghcr.io/openhands/agent-server:1.58.2-python"  # matches the pinned SDK; digest goes in the run record
LOCAL_MODEL = "openai/RadixArk/Qwen3.8-27B-NVFP4"
LOCAL_BASE_URL = "http://host.containers.internal:8000/v1"  # the host's vLLM, seen from a rootless container


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


def _image_digest(image: str) -> str:
    out = subprocess.run(["podman", "image", "inspect", image, "--format", "{{.Digest}}"],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _checkout(repo: str, base: str, dest: Path) -> str:
    subprocess.run(["git", "clone", "--quiet", repo, str(dest)], check=True)
    _git(dest, "checkout", "--quiet", "--detach", base)
    return _git(dest, "rev-parse", "HEAD").strip()


# Interpreter and tool caches are never part of a change; they are excluded from the patch, not counted as edits.
CACHE_EXCLUDES = [":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/.pytest_cache/**",
                  ":(exclude,glob)**/node_modules/**", ":(exclude,glob)**/.mypy_cache/**"]


def _patch_against(base_sha: str, project: Path) -> str:
    _git(project, "add", "-A", "--", ".", *CACHE_EXCLUDES)
    return _git(project, "diff", "--cached", "--binary", base_sha)


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


def run(item_path: Path, out: Path, model: str | None, direct: bool, max_iterations: int, timeout_s: float) -> dict:
    item = json.loads(item_path.read_text())
    root = item_path.parent
    if "prompt_file" in item:  # the agent receives only this text (protocol: no other item field reaches any arm)
        item["prompt"] = (root / item["prompt_file"]).read_text()
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
    project = work / "project"
    # An item may name a prepared image (agent-server base + the repository's dependencies at its base commit),
    # so the agent and the referee see the same toolchain.
    image = item.get("agent_image", AGENT_IMAGE)
    started = datetime.now(timezone.utc)
    job = f"{item['id']}-{started.strftime('%Y%m%dT%H%M%SZ')}"
    record: dict = {
        "arm": "C-openhands",
        "item": item["id"],
        "job": job,
        "started_at": started.isoformat(timespec="seconds"),
        "route": "direct" if direct else "gateway",
        "agent_image": image,
        "agent_image_digest": _image_digest(image),
        "max_iterations": max_iterations,
    }
    try:
        record["base"] = base_sha = _checkout(repo, item["base"], project)
        t0 = time.monotonic()
        if direct:  # debugging only: the sandbox reaches vLLM itself and nothing is counted outside the SDK
            record["model"] = model or LOCAL_MODEL
            llm = LLM(usage_id="builder", model=record["model"], base_url=LOCAL_BASE_URL,
                      api_key=SecretStr("local-no-key"), num_retries=2)
            _converse(item, image, project, llm, None, max_iterations, timeout_s, record)
        else:  # measured runs: internal network, per-job key, usage counted at the gateway
            gw_model = model or gateway.DEFAULT_MODEL
            record["model"] = gw_model
            gateway.up()
            with gateway.job_key(job, [gw_model]) as key:
                llm = LLM(usage_id="builder", model=f"openai/{gw_model}", base_url=gateway.INTERNAL_URL,
                          api_key=SecretStr(key), num_retries=2)
                _converse(item, image, project, llm, gateway.INTERNAL_NET, max_iterations, timeout_s, record)
            record.update(gateway.usage(job, started))
        record["wall_s"] = round(time.monotonic() - t0, 1)
        patch = _patch_against(base_sha, project)
        patch_file = out.parent / f"{item['id']}-{record['started_at'].replace(':', '')}.patch"
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
    p.add_argument("--max-iterations", type=int, default=60)
    p.add_argument("--timeout", type=float, default=1800)
    a = p.parse_args()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    print(json.dumps(run(a.item.resolve(), a.out.resolve(), a.model, a.direct, a.max_iterations, a.timeout),
                     indent=2))


if __name__ == "__main__":
    main()
