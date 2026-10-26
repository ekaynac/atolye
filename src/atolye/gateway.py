"""The model gateway (front filter + agentgateway) and per-job virtual keys.

Sandboxes join only `atolye-internal`, where the front filter is the one reachable service. The filter forwards only
allowlisted headers and redacts configured secrets in responses; behind it, on a second internal network,
agentgateway holds provider keys, checks per-job virtual keys and counts usage. Each job gets its own virtual key,
added to the gateway's config file before the job and removed after it; usage is read back from the gateway's JSON
access log by job id.
"""

from __future__ import annotations

import fcntl
import json
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

IMAGE = "cr.agentgateway.dev/agentgateway:v1.6.0"
FRONT_IMAGE = "ghcr.io/openhands/agent-server:1.58.2-python"  # any Python 3.10+ image; this one is already pulled
NAME = "atolye-gw"
FRONT = "atolye-front"
DEFAULT_MODEL = "qwen3.8-27b"  # the gateway's name for the local vLLM model (stack/gateway/config.yaml)
INTERNAL_NET = "atolye-internal"  # sandboxes + front filter
GW_NET = "atolye-gwnet"  # front filter + agentgateway; sandboxes are never on it
HOST_PORT = 14000
INTERNAL_URL = f"http://{FRONT}:4000/v1"  # what sandboxes use
HOST_URL = f"http://127.0.0.1:{HOST_PORT}/v1"  # what the runner uses to probe the gateway itself
REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "stack" / "gateway" / "config.yaml"
FRONT_SCRIPT = REPO / "stack" / "front" / "front_filter.py"
STATE = REPO / "runs" / "gw"
KEYS = STATE / "keys.json"
REDACT = STATE / "redact.txt"  # provider secrets the front filter must never let through (one per line)


def _podman(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["podman", *args], capture_output=True, text=True, check=check)


def _render(keys: list[dict]) -> None:
    """Write the live config: the template with the current key list (JSON is valid YAML).

    Written in place, not renamed: the container watches this exact inode through a bind mount.
    """
    text = TEMPLATE.read_text().replace("      keys: []", f"      keys: {json.dumps(keys)}")
    (STATE / "config.yaml").write_text(text)


@contextmanager
def _locked_keys() -> Iterator[list[dict]]:
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "keys.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        keys = json.loads(KEYS.read_text()) if KEYS.exists() else []
        yield keys
        KEYS.write_text(json.dumps(keys))
        _render(keys)


def up() -> None:
    """Start the front filter and the gateway if they are not running (idempotent)."""
    for net, flags in ((INTERNAL_NET, ["--internal"]), (GW_NET, ["--internal"])):
        if _podman("network", "exists", net, check=False).returncode != 0:
            _podman("network", "create", *flags, net)
    (STATE / "data").mkdir(parents=True, exist_ok=True)
    REDACT.touch(exist_ok=True)
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    gid = subprocess.run(["id", "-g"], capture_output=True, text=True).stdout.strip()
    user = ["--user", f"{uid}:{gid}", "--userns=keep-id"]
    if _podman("container", "exists", NAME, check=False).returncode != 0:
        with _locked_keys():
            pass  # renders the config with the current (normally empty) key list
        _podman("run", "-d", "--name", NAME, *user, "--network", "podman", "--network", GW_NET,
                "-p", f"127.0.0.1:{HOST_PORT}:4000",
                "-v", f"{STATE / 'config.yaml'}:/config.yaml:Z", "-v", f"{STATE / 'data'}:/data:Z",
                IMAGE, "-f", "/config.yaml")
    if _podman("container", "exists", FRONT, check=False).returncode != 0:
        _podman("run", "-d", "--name", FRONT, *user, "--network", INTERNAL_NET, "--network", GW_NET,
                "-e", f"UPSTREAM=http://{NAME}:4000", "-e", "SECRETS_FILE=/redact.txt",
                "-v", f"{REDACT}:/redact.txt:ro,Z", "-v", f"{FRONT_SCRIPT}:/front_filter.py:ro,Z",
                "--entrypoint", "python3", FRONT_IMAGE, "/front_filter.py")


def down() -> None:
    for name in (FRONT, NAME):  # one call each: podman refuses the whole batch if one name does not exist
        _podman("rm", "-f", "-t", "5", name, check=False)


def _probe(key: str) -> int:
    req = urllib.request.Request(f"{HOST_URL}/models", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return 0


@contextmanager
def job_key(job: str, models: list[str], ready_timeout_s: float = 30.0) -> Iterator[str]:
    """A virtual key that exists only for the job; it is revoked when the block ends, whatever happens."""
    key = f"vk-{secrets.token_hex(24)}"
    with _locked_keys() as keys:
        keys.append({"key": key, "metadata": {"job": job}, "allowedModels": models})
    try:
        deadline = time.monotonic() + ready_timeout_s
        while _probe(key) != 200:  # the gateway applies config changes asynchronously
            if time.monotonic() > deadline:
                raise TimeoutError("gateway did not accept the new job key")
            time.sleep(0.5)
        yield key
    finally:
        with _locked_keys() as keys:
            keys[:] = [k for k in keys if k["key"] != key]


def usage(job: str, since: datetime) -> dict:
    """Sum the gateway's LLM access-log lines for one job since a time."""
    logs = _podman("logs", "--since", since.astimezone(timezone.utc).isoformat(), NAME, check=False)
    totals = {"gw_requests": 0, "gw_errors": 0, "gw_input_tokens": 0, "gw_output_tokens": 0,
              "gw_cached_input_tokens": 0}
    for line in (logs.stdout + logs.stderr).splitlines():
        if '"job"' not in line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("job") != job:
            continue
        totals["gw_requests"] += 1
        if int(rec.get("http.status", 0)) >= 400:
            totals["gw_errors"] += 1
        totals["gw_input_tokens"] += int(rec.get("gen_ai.usage.input_tokens") or 0)
        totals["gw_output_tokens"] += int(rec.get("gen_ai.usage.output_tokens") or 0)
        totals["gw_cached_input_tokens"] += int(rec.get("cached_input_tokens") or 0)
    return totals


def main() -> None:
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "up"
    {"up": up, "down": down}[cmd]()


if __name__ == "__main__":
    main()
