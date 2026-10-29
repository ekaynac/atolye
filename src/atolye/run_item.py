"""Run one item headless and append one record line.

An item is a directory: `item.json` (a P-A referee item: repo, base, image, optional setup, tests, hidden acceptance)
and `requirement.md`, the only text the agent receives (protocol: no other item field reaches any arm).

Usage: atolye-run ITEM_DIR --out runs.jsonl [--worker openhands|mini] [--model M] [--max-iterations N] [--timeout S]
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import SecretStr

from openhands.sdk import LLM, Conversation
from openhands.sdk.mcp.config import MCPBearerAuthCredential, MCPServer
from openhands.tools.preset.default import get_default_agent

from atolye import gateway, vllm_metrics
from atolye.podman import ServerSpec, agent_image, agent_server, remove_tree, run_setup
from atolye.referee import binary as referee_binary
from atolye.referee import referee

# 100 turns: at 50, 6 of arm C's 10 local rejections were the cap, and at 100 4 of the 5 cap-bound cells were accepted
# (stage-2 three-way report §4b). The wall limit, 60 min, stays the budget limit. Stage-2's comparison runs used 50,
# the same as adc-dev's T1-cloud cells; pass --max-iterations 50 to reproduce them.
DEFAULT_MAX_ITERATIONS = 100
DEFAULT_TIMEOUT_S = 3600
# The same task wrapper as adc-dev's T1-cloud OpenHands cells, so local and cloud cells get the same prompt: the prefix,
# a blank line, requirement.md, a newline, then the closing line (`harness/run_openhands.py:42-43`; coordination
# §6.22). Stage 2's arm C and T3 rows ran without the closing line (prompt_version `t1-cloud-prefix`).
OPENHANDS_PREFIX = "The repository is at /workspace. Work only inside it."
OPENHANDS_SUFFIX = "Make the change in the source files. When you are done, call the finish tool."
# T3 only: the lines that tell the agent the memory exists and how to read it cheaply. Kütüphane's guide
# (docs/guide/agent-memory.md §4, kutuphane#121): search with max_chars, then open only the passages needed. The first
# T3 (2026-10-27) used only the first sentence (prompt_version `+memory-hint`); full passages in every search result
# cost +50 % input tokens.
MEMORY_HINT = ("The project's design documents and decision log are in a library you can query with the "
               "search_library and open_citation tools. Call search_library with max_chars: 300, then call "
               "open_citation only for the passages that you need.")
KUTUPHANE_TOOLS = ("search_library", "open_citation", "ask_library", "read_document", "list_books")


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


def _exclude_setup_outputs(project: Path, before: set[str], host_gitdir: Path) -> list[str]:
    """What the item's setup step created (a module cache, say) is the environment, not the agent's change."""
    created = sorted(p.name for p in project.iterdir() if p.name not in before)
    if created:
        (host_gitdir / "info").mkdir(exist_ok=True)
        with open(host_gitdir / "info" / "exclude", "a") as f:
            f.writelines(f"/{name}\n" for name in created)
    return created


def _patch_against(base_sha: str, project: Path, host_gitdir: Path) -> str:
    _host_git(host_gitdir, project, "read-tree", base_sha)  # start the index from base, not from the sandbox
    _host_git(host_gitdir, project, "add", "-A", "--", ".", *CACHE_EXCLUDES)
    return _host_git(host_gitdir, project, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv",
                     base_sha)


def _leading_env(argv: list[str] | None) -> dict[str, str]:
    """`env K=V ... cmd`: the K=V pairs. The agent's commands get the same environment the referee tests with."""
    out: dict[str, str] = {}
    if argv and argv[0] == "env":
        for arg in argv[1:]:
            if "=" not in arg or arg.startswith("-"):
                break
            k, v = arg.split("=", 1)
            out[k] = v
    return out


def _last_agent_event(events) -> str:
    for ev in reversed(list(events)):
        if getattr(ev, "source", None) == "agent":
            action = getattr(ev, "action", None)
            return type(action).__name__ if action is not None else type(ev).__name__
    return "none"


def _memory_calls(events) -> int:
    """Agent actions that called a Kütüphane tool (the SDK may prefix MCP tool names with the server name)."""
    return sum(1 for ev in events if getattr(ev, "source", None) == "agent"
               and str(getattr(ev, "tool_name", "") or "").endswith(KUTUPHANE_TOOLS))


@dataclass(frozen=True)
class Memory:
    """Kütüphane over MCP for the OpenHands agent (stage-2 T3): one scoped, read-only agent token."""

    url: str
    token_file: Path
    book: str
    commit: str

    def server(self) -> MCPServer:
        return MCPServer(url=self.url, transport="http", auth=MCPBearerAuthCredential(
            strategy="bearer", value=SecretStr(self.token_file.read_text().strip())))


def _converse(prompt: str, image: str, project: Path, env: dict[str, str], llm: LLM, network: str | None,
              max_iterations: int, timeout_s: float, record: dict, memory: Memory | None = None,
              trajectory: Path | None = None) -> None:
    agent = get_default_agent(llm=llm, cli_mode=True)
    if memory:
        agent = agent.model_copy(update={"mcp_config": {"kutuphane": memory.server()}})
    with agent_server(ServerSpec(image=image, project_dir=project, env=env, network=network)) as ws:
        conv = Conversation(agent=agent, workspace=ws, max_iteration_per_run=max_iterations)
        try:
            conv.send_message(prompt)
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
                memory_calls=_memory_calls(conv.state.events) if memory else 0,
            )
            if trajectory:  # the events only: the agent config (with the MCP bearer token) is not written
                with trajectory.open("w") as f:
                    for ev in conv.state.events:
                        f.write(json.dumps(ev.model_dump(mode="json")) + "\n")
        finally:
            conv.close()


def run(item_dir: Path, out: Path, model: str | None, max_iterations: int, timeout_s: float,
        worker: str = "mini", repeat: int = 0, memory: Memory | None = None) -> dict:
    if memory and worker != "openhands":
        raise ValueError("memory (Kütüphane MCP) needs the OpenHands worker; mini-swe-agent has no MCP client")
    item_file = item_dir / "item.json"
    item = json.loads(item_file.read_text())
    prompt = (item_dir / "requirement.md").read_text()
    referee_binary()  # fail now, not after an hour of agent time
    repo = str((item_dir / item["repo"]).resolve())
    started = datetime.now(timezone.utc)
    # Unique even for two runs of one item in the same second: usage is summed by this id.
    job = f"{item_dir.name}-{started.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    gw_model = model or gateway.DEFAULT_MODEL
    record: dict = {
        "arm": ("C-openhands+kutuphane" if memory else "C-openhands") if worker == "openhands" else f"C-{worker}",
        "worker": worker,
        "item": item_dir.name,
        "job": job,
        "repeat": repeat,
        "started_at": started.isoformat(timespec="seconds"),
        "model": gw_model,
        "item_image": item["image"],
        "max_iterations": max_iterations,
        "timeout_s": timeout_s,
        "memory": {"url": memory.url, "book": memory.book, "kutuphane_commit": memory.commit} if memory else None,
    }
    env = {**_leading_env(item.get("setup")), **_leading_env(item["test_command"])}
    record["agent_env"] = env
    work = Path(tempfile.mkdtemp(prefix=f"atolye-{item_dir.name}-"))
    project, host_gitdir = work / "project", work / "host.git"
    try:
        image = agent_image(item["image"]) if worker == "openhands" else item["image"]
        record.update(agent_image=image, agent_image_digest=_image_digest(image))
        record["base"] = base_sha = _checkout(repo, item["base"], project, host_gitdir)
        if item.get("setup"):
            before = {p.name for p in project.iterdir()}
            run_setup(item["image"], item["setup"], project)
            record["setup_outputs"] = _exclude_setup_outputs(project, before, host_gitdir)
        gateway.up()
        before_metrics = vllm_metrics.snapshot()
        with gateway.job_key(job, [gw_model], ttl_s=timeout_s + 600) as key:
            t0 = time.monotonic()  # the agent's time only: no image build, setup, gateway start-up or key propagation
            if worker == "mini":  # agent loop on the host, commands in a container with no network
                from atolye import mini_worker  # optional extra: `uv sync --extra mini`

                record["trajectory"] = str(out.parent / f"{job}.traj.json")
                mini_worker.converse(prompt, image, project, env, gw_model, gateway.HOST_URL, key, max_iterations,
                                     timeout_s, record, Path(record["trajectory"]))
                # mini's SWE-bench protocol writes its submission to patch.txt; that is the harness's file, not a change
                submission = project / "patch.txt"
                in_base = subprocess.run(["git", f"--git-dir={host_gitdir}", "cat-file", "-e", f"{base_sha}:patch.txt"],
                                         capture_output=True).returncode == 0
                if submission.is_file() and not in_base:
                    submission.unlink()
            else:  # OpenHands: the sandbox reaches only the front filter
                llm = LLM(usage_id="builder", model=f"openai/{gw_model}", base_url=gateway.INTERNAL_URL,
                          api_key=SecretStr(key), num_retries=2 if gw_model == gateway.DEFAULT_MODEL else 0)
                record["prompt_version"] = "t1-cloud" + ("+memory-snippets" if memory else "")
                hint = f"{MEMORY_HINT}\n\n" if memory else ""
                task = f"{OPENHANDS_PREFIX}\n\n{hint}{prompt}\n{OPENHANDS_SUFFIX}"
                record["trajectory"] = str(out.parent / f"{job}.events.jsonl")
                _converse(task, image, project, env, llm, gateway.INTERNAL_NET, max_iterations, timeout_s, record,
                          memory, Path(record["trajectory"]))
            record["wall_s"] = round(time.monotonic() - t0, 1)
        record.update(gateway.usage(job, started))
        if gw_model == gateway.DEFAULT_MODEL:  # the local vLLM route: read its prefix-cache counters
            record.update(vllm_metrics.delta(before_metrics, vllm_metrics.snapshot(), record["gw_input_tokens"]))
        patch = _patch_against(base_sha, project, host_gitdir)
        patch_file = out.parent / f"{job}.patch"
        patch_file.write_text(patch)
        record.update(patch=str(patch_file), patch_lines=patch.count("\n"))
        record["referee"] = referee(item_file, patch_file, out.parent)
    finally:
        remove_tree(work)
    with out.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return record


def main() -> None:
    p = argparse.ArgumentParser(prog="atolye-run")
    p.add_argument("item", type=Path, help="an item directory: item.json (P-A referee item) + requirement.md")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--model", default=None, help="gateway model name (default: the gateway's local model)")
    p.add_argument("--worker", choices=("openhands", "mini"), default="mini",
                   help="mini (mini-swe-agent, the default for well-specified items on the local model) or openhands "
                        "(when the task needs MCP tools such as Kütüphane, a browser or the Agent Server API)")
    p.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    add_memory_args(p)
    a = p.parse_args()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    print(json.dumps(run(a.item.resolve(), a.out.resolve(), a.model, a.max_iterations, a.timeout, a.worker,
                         memory=memory_from_args(a)), indent=2))


def add_memory_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--memory-url", help="Kütüphane MCP URL as the sandbox sees it (enables the memory; T3)")
    p.add_argument("--memory-token-file", type=Path, help="file holding the scoped agent token (never printed)")
    p.add_argument("--memory-book", help="the one book the token may read (recorded)")
    p.add_argument("--memory-commit", help="the pinned Kütüphane commit (recorded)")


def memory_from_args(a: argparse.Namespace) -> Memory | None:
    if not a.memory_url:
        return None
    if not (a.memory_token_file and a.memory_book and a.memory_commit):
        raise SystemExit("--memory-url needs --memory-token-file, --memory-book and --memory-commit")
    return Memory(a.memory_url, a.memory_token_file, a.memory_book, a.memory_commit)


if __name__ == "__main__":
    main()
