"""Rootless Podman: the OpenHands Agent Server per job, derived agent images per item, and the item's setup step.

shortcut: a small stand-in for a Podman `BaseWorkspace` upstream (R19 upstream-first); replace once one exists.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import socket
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from openhands.sdk.workspace import RemoteWorkspace

CONTAINER_PORT = 8000
WORKDIR = "/workspace"  # where the referee (P-A) runs the item's commands, so paths in the item's env match
AGENT_IMAGE_RECIPE = Path(__file__).resolve().parents[2] / "stack" / "agent-image" / "Containerfile"


@dataclass(frozen=True)
class ServerSpec:
    image: str
    project_dir: Path
    env: dict[str, str] = field(default_factory=dict)
    network: str | None = None  # e.g. the internal network where only the model gateway is reachable
    health_timeout_s: float = 120.0


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _podman(*args: str) -> str:
    proc = subprocess.run(["podman", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"podman {args[0]} failed: {proc.stderr.strip()[-2000:]}")
    return proc.stdout.strip()


def _user() -> list[str]:
    return ["--userns=keep-id", f"--user={os.getuid()}:{os.getgid()}"]


def remove_tree(path: Path) -> None:
    """Remove a directory that a container wrote into: its files can be owned by mapped sub-uids."""
    subprocess.run(["podman", "unshare", "rm", "-rf", "--", str(path)], check=True, capture_output=True)


def agent_image(item_image: str) -> str:
    """The item's image plus the OpenHands agent server (stack/agent-image/Containerfile), built once per pair."""
    recipe = AGENT_IMAGE_RECIPE.read_bytes()
    tag = "localhost/atolye-agent:" + hashlib.sha256(recipe + item_image.encode()).hexdigest()[:16]
    if subprocess.run(["podman", "image", "exists", tag]).returncode != 0:
        _podman("build", "-q", "--build-arg", f"ITEM_IMAGE={item_image}", "-t", tag, "-f", str(AGENT_IMAGE_RECIPE),
                str(AGENT_IMAGE_RECIPE.parent))
    return tag


def run_setup(image: str, argv: list[str], project_dir: Path, timeout_s: float = 1800) -> None:
    """The item's setup step (for example a dependency download), with the network, as P-A runs it, so the agent's
    sandbox (no network) starts from the same prepared workspace the referee tests in."""
    proc = subprocess.run(["podman", "run", "--rm", *_user(), "-v", f"{project_dir}:{WORKDIR}:Z", "-w", WORKDIR,
                           "--entrypoint", argv[0], image, *argv[1:]],
                          capture_output=True, text=True, timeout=timeout_s)
    if proc.returncode != 0:
        raise RuntimeError(f"item setup failed ({proc.returncode}): {(proc.stdout + proc.stderr)[-1500:]}")


def _wait_healthy(url: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=2) as r:
                if r.status == 200:
                    return
        except OSError:
            pass
        time.sleep(1)
    raise TimeoutError(f"agent server at {url} not healthy after {timeout_s:.0f}s")


@contextmanager
def agent_server(spec: ServerSpec) -> Iterator[RemoteWorkspace]:
    """Start the container, yield a connected workspace, and always remove the container."""
    port = _free_loopback_port()
    key = secrets.token_hex(32)
    env_flags = [f for k, v in spec.env.items() for f in ("-e", f"{k}={v}")]
    cid = _podman(
        "run", "-d", "--rm", *_user(),  # host uid owns the mounted checkout inside the container
        "--security-opt", "no-new-privileges",
        "--pids-limit", "1024",
        "--memory", "8g",
        "-p", f"127.0.0.1:{port}:{CONTAINER_PORT}",
        *(["--network", spec.network] if spec.network else []),
        "-e", f"OH_SESSION_API_KEYS_0={key}",
        *env_flags,
        "-v", f"{spec.project_dir}:{WORKDIR}:Z",
        spec.image,
        "--host", "0.0.0.0", "--port", str(CONTAINER_PORT),
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_healthy(url, spec.health_timeout_s)
        yield RemoteWorkspace(host=url, api_key=key, working_dir=WORKDIR)
    finally:
        subprocess.run(["podman", "rm", "-f", "-t", "5", cid], capture_output=True)
