"""A minimal stand-in referee: hidden acceptance on a clean checkout, in a container with no network.

shortcut: P-A (`adc dev referee`, language-agnostic, with the full integrity guard) replaces this for measured runs;
this one exists so the harness can be smoke-tested end to end before P-A lands.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from atolye.podman import remove_tree

PROTECTED = ["conftest.py", "*/conftest.py", "pytest.ini", "jest.config.*", "vitest.config.*", "phpunit.xml*",
             "go.sum", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "uv.lock", "composer.lock"]


def _touched(patch_file: Path) -> list[str]:
    out = subprocess.run(["git", "apply", "--numstat", str(patch_file)], check=True, capture_output=True, text=True)
    return [line.split("\t", 2)[2] for line in out.stdout.splitlines() if line.count("\t") >= 2]


def referee(item: dict, root: Path, repo: str, base_sha: str, patch_file: Path) -> dict:
    spec = item["referee"]
    # Measured items keep their hidden acceptance outside this public repository (R28): `"private": true`
    # resolves `acceptance_dir` under $ATOLYE_ACCEPTANCE.
    acc_root = Path(os.environ.get("ATOLYE_ACCEPTANCE", "")) if spec.get("private") else root
    acc_dir = (acc_root / spec["acceptance_dir"]).resolve()
    if not acc_dir.is_dir():
        raise FileNotFoundError(f"acceptance directory not found: {acc_dir}")
    hidden = [p.relative_to(acc_dir).as_posix() for p in acc_dir.rglob("*") if p.is_file()]
    if patch_file.stat().st_size == 0:
        return {"verdict": "failed", "reason": "empty patch"}
    touched = _touched(patch_file)
    bad = sorted({t for t in touched for pat in [*PROTECTED, *hidden] if fnmatch.fnmatch(t, pat)})
    if bad:
        return {"verdict": "refused", "reason": "patch touches protected or acceptance paths", "paths": bad}
    work = Path(tempfile.mkdtemp(prefix="atolye-ref-"))
    try:
        subprocess.run(["git", "clone", "--quiet", repo, str(work)], check=True)
        subprocess.run(["git", "-C", str(work), "checkout", "--quiet", "--detach", base_sha], check=True)
        applied = subprocess.run(["git", "-C", str(work), "apply", "--whitespace=nowarn", str(patch_file)],
                                 capture_output=True, text=True)
        if applied.returncode != 0:
            return {"verdict": "failed", "reason": "patch does not apply", "detail": applied.stderr[-300:]}
        for rel in hidden:
            dest = work / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(acc_dir / rel, dest)
        run = subprocess.run(
            ["podman", "run", "--rm", "--network", "none", "--userns=keep-id", f"--user={os.getuid()}:{os.getgid()}",
             "-v", f"{work}:/w:Z", "-w", "/w",
             "--entrypoint", spec["test_cmd"][0], spec["image"], *spec["test_cmd"][1:]],
            capture_output=True, text=True, timeout=spec.get("timeout_s", 600))
        return {"verdict": "accepted" if run.returncode == 0 else "failed", "exit": run.returncode,
                "tail": (run.stdout + run.stderr)[-600:]}
    finally:
        remove_tree(work)
