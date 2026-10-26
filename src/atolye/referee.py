"""The stand-in referee: hidden acceptance on a clean checkout, in a container with no network.

shortcut: P-A (`adc dev referee`, language-agnostic, with ADC's integrity guard) replaces this for measured runs;
this one exists so the harness can be tested end to end before P-A lands.

What makes a patch `refused` before any test runs:
- it touches a path outside the item's `allowed_paths` (old and new names of renames and copies both count);
- it touches a protected path anywhere: test-runner config, lockfiles, interpreter start-up hooks, module shadows;
- it adds a symlink or a submodule, or a path with `..` or an absolute path;
- it touches an acceptance path.
The hidden tests are mounted read-only, so nothing the patch runs during the test (a build plugin, a conftest) can
rewrite them.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from atolye.podman import remove_tree

# Protected wherever they appear (matched against the full path and the base name).
PROTECTED = [
    # test-runner and build configuration
    "conftest.py", "pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "noxfile.py",
    "jest.config.*", "vitest.config.*", "vitest.workspace.*", "vite.config.*", "uno.config.*", "playwright.config.*",
    "babel.config.*", ".babelrc*", "tsconfig*.json", "package.json", ".npmrc", "phpunit.xml*", "go.mod",
    # lockfiles
    "go.sum", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "uv.lock", "composer.lock",
    # interpreter start-up hooks and module shadows
    "sitecustomize.py", "usercustomize.py", "*.pth", "pytest.py", "_pytest", "py.py",
]
_MODE = re.compile(rb"^(?:new file mode|new mode|old mode|deleted file mode) (\d+)$", re.M)


def acceptance_dir(item: dict, root: Path) -> Path:
    """The item's hidden-acceptance directory; raises if it is not there.

    Measured items keep it outside this public repository (R28): `"private": true` resolves `acceptance_dir` under
    $ATOLYE_ACCEPTANCE.
    """
    spec = item["referee"]
    if spec.get("private"):
        if not os.environ.get("ATOLYE_ACCEPTANCE"):
            raise FileNotFoundError("ATOLYE_ACCEPTANCE is not set, and this item's acceptance is private")
        acc_root = Path(os.environ["ATOLYE_ACCEPTANCE"])
    else:
        acc_root = root
    acc = (acc_root / spec["acceptance_dir"]).resolve()
    if not acc.is_dir():
        raise FileNotFoundError(f"acceptance directory not found: {acc}")
    return acc


def _unquote(name: bytes) -> str:
    """Git's C-style quoting of unusual path names ("a\\tb"), undone."""
    if name.startswith(b'"') and name.endswith(b'"'):
        return name[1:-1].decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
    return name.decode("utf-8", "replace")


_RENAME = re.compile(rb"^(?:rename|copy) (?:from|to) (.+)$", re.M)


def touched_paths(patch_file: Path) -> list[str]:
    """Every path a patch touches. git's own parser gives the new names; rename and copy headers add the old ones
    (`git apply --numstat` reports only the destination of a rename)."""
    out = subprocess.run(["git", "apply", "--numstat", "-z", str(patch_file)], check=True, capture_output=True).stdout
    fields, paths, i = out.split(b"\0"), [], 0
    while i < len(fields) and fields[i]:
        stat = fields[i].split(b"\t")
        if len(stat) == 3 and stat[2]:  # "added\tdeleted\tpath"
            paths.append(stat[2].decode())
            i += 1
        else:  # "added\tdeleted\t" then old and new names as the next two fields
            paths += [fields[i + 1].decode(), fields[i + 2].decode()]
            i += 3
    paths += [_unquote(m) for m in _RENAME.findall(patch_file.read_bytes())]
    return paths


def _protected(path: str) -> bool:
    p = PurePosixPath(path)
    return any(fnmatch.fnmatch(part, pat) for part in p.parts for pat in PROTECTED)


def check_patch(patch_file: Path, allowed: list[str], hidden_roots: list[str]) -> list[str]:
    """Reasons to refuse the patch (empty when it may be tested)."""
    reasons = []
    modes = {m.decode() for m in _MODE.findall(patch_file.read_bytes())}
    if modes & {"120000", "160000"}:
        reasons.append("adds a symlink or a submodule")
    for path in touched_paths(patch_file):
        p = PurePosixPath(path)
        if p.is_absolute() or ".." in p.parts:
            reasons.append(f"unsafe path: {path}")
        elif p.parts and p.parts[0] in hidden_roots:
            reasons.append(f"touches an acceptance path: {path}")
        elif _protected(path):
            reasons.append(f"touches a protected path: {path}")
        elif allowed and not any(fnmatch.fnmatch(path, pat) for pat in allowed):
            reasons.append(f"outside the allowed paths: {path}")
    return sorted(set(reasons))


def referee(item: dict, root: Path, repo: str, base_sha: str, patch_file: Path) -> dict:
    spec = item["referee"]
    acc = acceptance_dir(item, root)
    hidden_roots = sorted(e.name for e in acc.iterdir())  # each is mounted read-only at /w/<name>
    if patch_file.stat().st_size == 0:
        return {"verdict": "failed", "reason": "empty patch"}
    reasons = check_patch(patch_file, spec.get("allowed_paths", []), hidden_roots)
    if reasons:
        return {"verdict": "refused", "reasons": reasons}
    work = Path(tempfile.mkdtemp(prefix="atolye-ref-"))
    try:
        subprocess.run(["git", "clone", "--quiet", repo, str(work)], check=True)
        subprocess.run(["git", "-C", str(work), "checkout", "--quiet", "--detach", base_sha], check=True)
        applied = subprocess.run(["git", "-C", str(work), "apply", "--whitespace=nowarn", str(patch_file)],
                                 capture_output=True, text=True)
        if applied.returncode != 0:
            return {"verdict": "failed", "reason": "patch does not apply", "detail": applied.stderr[-300:]}
        mounts = []
        for name in hidden_roots:
            if (work / name).exists():
                raise RuntimeError(f"acceptance root {name!r} already exists in the repository at base")
            mounts += ["-v", f"{acc / name}:/w/{name}:ro,Z"]
        run = subprocess.run(
            ["podman", "run", "--rm", "--network", "none", "--userns=keep-id", f"--user={os.getuid()}:{os.getgid()}",
             "-v", f"{work}:/w:Z", *mounts, "-w", "/w",
             "--entrypoint", spec["test_cmd"][0], spec["image"], *spec["test_cmd"][1:]],
            capture_output=True, text=True, timeout=spec.get("timeout_s", 600))
        return {"verdict": "accepted" if run.returncode == 0 else "failed", "exit": run.returncode,
                "tail": (run.stdout + run.stderr)[-600:]}
    finally:
        remove_tree(work)
