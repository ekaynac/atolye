"""Run a set of cells (item x worker x repeat) one at a time, and honour the shared GPU marker.

Usage: atolye-campaign --items DIR [DIR ...] --workers openhands mini --repeats 1 --out runs/arm-c.jsonl

The marker (/home/mega/adc-research/GPU-NOW, stage-2 coordination 2026-10-27) holds one line
`<session> <job> <start time>`. The campaign refuses to start while another session's line is in it, writes its own
line while it runs, and removes it when it ends. A cell already recorded in --out (same item, worker, repeat) is
skipped, so an interrupted campaign resumes at the next cell boundary.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path

from atolye import gateway
from atolye.run_item import (DEFAULT_MAX_ITERATIONS, DEFAULT_TIMEOUT_S, add_memory_args, append_record,
                             memory_from_args, run)

MARKER = Path("/home/mega/adc-research/GPU-NOW")
STOP = Path("/home/mega/adc-research/GPU-STOP")  # any session or the operator creates it to stop at a cell boundary
SESSION = "atolye"
# Resident inference servers that share the GPU by design; anything else (a training run) means the GPU is not free.
INFERENCE_SERVERS = ("VLLM::", "ollama/llama-server")


def _foreign_gpu_jobs() -> list[str]:
    """GPU compute processes other than the resident inference servers. A job that writes no marker is still seen
    here: on 2026-10-27 the training resumed without one and 18 runs shared the GPU with it."""
    out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name", "--format=csv,noheader"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return [f"nvidia-smi failed: {out.stderr.strip()[:200]}"]
    return [line.strip() for line in out.stdout.splitlines()
            if line.strip() and not any(name in line for name in INFERENCE_SERVERS)]

# A cell's sandbox may use up to 8 GiB (podman --memory 8g) plus the host-side runner. The host is shared and its GPU
# memory is system memory: on 2026-10-31 an OOM cascade (10:59–11:48) killed other users' services while a T3 rerun
# ran beside a training job. So no cell starts unless this much memory is available.
MIN_AVAILABLE_GIB = float(os.environ.get("ATOLYE_MIN_AVAILABLE_GIB", "16"))


def _available_gib() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024 / 1024
    return 0.0



def _done(out: Path) -> set[tuple[str, str, int]]:
    """Cells already recorded. An infrastructure error is not a result, so its cell runs again on resume."""
    if not out.exists():
        return set()
    recs = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    return {(r["item"], r["worker"], int(r.get("repeat", 0))) for r in recs
            if not str(r.get("stop", "")).startswith("infra_error") and not r.get("gw_quota_errors")}


def main() -> None:
    p = argparse.ArgumentParser(prog="atolye-campaign")
    p.add_argument("--items", type=Path, nargs="+", required=True)
    p.add_argument("--workers", nargs="+", default=["mini"], choices=("openhands", "mini"),
                   help="default mini; add openhands to compare, or for tasks that need MCP tools")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--job", default="arm-C", help="the job name written into the marker")
    p.add_argument("--wait", action="store_true", help="wait until the marker is free instead of refusing")
    p.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--until", default=None, metavar="HH:MM",
                   help="start no new cell at or after this local time (same day); running cells finish")
    p.add_argument("--parallel", type=int, default=1,
                   help="cells at a time (default 1). Each cell is one sandbox of up to 8 GiB; the memory floor is "
                        "checked before every new cell")
    p.add_argument("--model", default=None,
                   help="gateway model name; a cloud model does not take the GPU marker (default: the local model)")
    add_memory_args(p)
    a = p.parse_args()
    memory = memory_from_args(a)
    a.out = a.out.resolve()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    gpu = a.model is None or a.model in gateway.LOCAL_MODELS
    if gpu:
        if a.wait:  # wait for the session before us in the GPU order to remove its line
            while MARKER.exists() and not MARKER.read_text().startswith(SESSION + " "):
                time.sleep(60)
        if MARKER.exists() and not MARKER.read_text().startswith(SESSION + " "):
            sys.exit(f"the GPU is in use: {MARKER} says {MARKER.read_text().strip()!r}")
        if STOP.exists() or (foreign := _foreign_gpu_jobs()):
            sys.exit(f"the GPU is not free: {STOP if STOP.exists() else foreign}")
        MARKER.write_text(f"{SESSION} {a.job} {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    try:
        done = _done(a.out)
        # repeat-major: every cell once before any cell twice
        cells = [(item, worker, repeat) for repeat in range(a.repeats) for item in a.items for worker in a.workers
                 if (item.name, worker, repeat) not in done]
        _run_cells(cells, a, gpu, memory)
    finally:
        if gpu and MARKER.exists() and MARKER.read_text().startswith(SESSION + " "):
            MARKER.unlink()


def _stop_reason(gpu: bool, until: str | None = None) -> str | None:
    """Why no new cell may start now (cells already running finish)."""
    if until and datetime.now().strftime("%H:%M") >= until:
        return f"--until {until} reached"
    if gpu and (STOP.exists() or (foreign := _foreign_gpu_jobs())):
        return f"{STOP if STOP.exists() else foreign}"
    if (avail := _available_gib()) < MIN_AVAILABLE_GIB:  # every campaign, cloud ones too
        return f"{avail:.1f} GiB available, below {MIN_AVAILABLE_GIB:g} GiB"
    return None


def _one_cell(item: Path, worker: str, repeat: int, a: argparse.Namespace, memory) -> dict:
    print(f"[{datetime.now():%H:%M}] {item.name} {worker} repeat {repeat}", flush=True)
    try:
        rec = run(item.resolve(), a.out, a.model, a.max_iterations, a.timeout, worker, repeat=repeat, memory=memory)
    except Exception as exc:  # infrastructure (setup, podman, referee binary): record it, go on
        rec = {"arm": f"C-{worker}", "worker": worker, "item": item.name, "repeat": repeat,
               "stop": f"infra_error: {type(exc).__name__}: {exc}"[:500], "referee": {"verdict": "could_not_run"}}
        append_record(a.out, rec)
    print(f"  -> {item.name} {worker} repeat {repeat}: {rec.get('referee', {}).get('verdict')} in "
          f"{rec.get('wall_s')} s", flush=True)
    return rec


def _run_cells(cells: list, a: argparse.Namespace, gpu: bool, memory) -> None:
    """Start cells in order, at most --parallel at a time. Stop conditions are checked before every start; a provider
    refusal stops new starts at once (never retried in a loop)."""
    width = max(1, a.parallel)
    pending, running, refused = list(cells), set(), False
    with ThreadPoolExecutor(max_workers=width) as pool:
        while pending or running:
            while pending and not refused and len(running) < width:
                if reason := _stop_reason(gpu, a.until):
                    print(f"[{datetime.now():%H:%M}] stopping new cells: {reason}", flush=True)
                    pending.clear()
                    break
                running.add(pool.submit(_one_cell, *pending.pop(0), a, memory))
                if width > 1:
                    time.sleep(5)  # stagger sandbox start-up
            if not running:
                break
            finished, running = wait(running, return_when=FIRST_COMPLETED)
            for fut in finished:
                rec = fut.result()
                if rec.get("gw_quota_errors") and not refused:
                    refused = True
                    print(f"[{datetime.now():%H:%M}] stopping new cells: the provider refused "
                          f"({rec['gw_quota_errors']} auth/quota errors)", flush=True)


if __name__ == "__main__":
    main()
