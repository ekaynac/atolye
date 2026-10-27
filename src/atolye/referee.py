"""The referee: P-A, `adc dev referee` (ekaynac/auto-dev-cycle, main 9ee8a40a), the one referee for every arm.

P-A takes an item (schemas/testing/referee-item.json), a patch against the item's base, and writes a verdict record:
`accepted`, `rejected`, `invalid_item` or `could_not_run`. It overlays the hidden acceptance in its own workspace,
restores protected and non-editable paths to base before testing, and runs the item's tests with no network.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

DEFAULT_BINARY = "/home/mega/atolye-acceptance/bin/adc-referee"


def binary() -> str:
    path = os.environ.get("ATOLYE_REFEREE", DEFAULT_BINARY)
    if not os.access(path, os.X_OK):
        raise FileNotFoundError(f"referee binary not found or not executable: {path} (set ATOLYE_REFEREE)")
    return path


def _judge(item_file: Path, patch_file: Path, record_dir: Path, attempt: int, timeout_s: float) -> dict:
    out = record_dir / f"{patch_file.stem}.verdict{attempt}.json"
    logs = record_dir / f"{patch_file.stem}.referee-logs{attempt}"  # P-A creates it and refuses an existing one
    proc = subprocess.run([binary(), "dev", "referee", "-item", str(item_file), "-patch", str(patch_file),
                           "-out", str(out), "-logs", str(logs)], capture_output=True, text=True, timeout=timeout_s)
    if not out.exists():
        return {"verdict": "could_not_run", "reason": f"referee exited {proc.returncode} without a record",
                "stderr": proc.stderr[-800:]}
    record = json.loads(out.read_text())
    return {"verdict": record.get("verdict"), "record": str(out), "logs": str(logs),
            **{k: record[k] for k in ("outside_editable", "protected_touched", "reason") if k in record}}


def referee(item_file: Path, patch_file: Path, record_dir: Path, timeout_s: float = 7200,
            attempts: int = 3, pause_s: float = 60) -> dict:
    """Judge one patch. `could_not_run` is an infrastructure result, never a model failure: judge again (as the
    adc-dev T1 runner does), up to `attempts` times. Every attempt's record stays next to the run's other records."""
    tried = []
    for attempt in range(1, attempts + 1):
        result = _judge(item_file, patch_file, record_dir, attempt, timeout_s)
        tried.append(result["verdict"])
        if result["verdict"] != "could_not_run":
            break
        time.sleep(pause_s)
    return {**result, "attempts": tried}
