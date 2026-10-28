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
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from atolye import gateway
from atolye.run_item import DEFAULT_MAX_ITERATIONS, DEFAULT_TIMEOUT_S, add_memory_args, memory_from_args, run

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
    p.add_argument("--workers", nargs="+", default=["openhands", "mini"], choices=("openhands", "mini"))
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--job", default="arm-C", help="the job name written into the marker")
    p.add_argument("--wait", action="store_true", help="wait until the marker is free instead of refusing")
    p.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--model", default=None,
                   help="gateway model name; a cloud model does not take the GPU marker (default: the local model)")
    add_memory_args(p)
    a = p.parse_args()
    memory = memory_from_args(a)
    a.out = a.out.resolve()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    gpu = a.model in (None, gateway.DEFAULT_MODEL)
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
        for repeat in range(a.repeats):  # repeat-major: every cell once before any cell twice
            for item in a.items:
                for worker in a.workers:
                    if (item.name, worker, repeat) in done:
                        continue
                    if gpu and (STOP.exists() or (foreign := _foreign_gpu_jobs())):  # stop at this cell boundary
                        print(f"[{datetime.now():%H:%M}] stopping: {STOP if STOP.exists() else foreign}", flush=True)
                        return
                    print(f"[{datetime.now():%H:%M}] {item.name} {worker} repeat {repeat}", flush=True)
                    try:
                        rec = run(item.resolve(), a.out, a.model, a.max_iterations, a.timeout, worker, repeat=repeat,
                                  memory=memory)
                    except Exception as exc:  # infrastructure (setup, podman, referee binary): record it, go on
                        rec = {"arm": f"C-{worker}", "worker": worker, "item": item.name, "repeat": repeat,
                               "stop": f"infra_error: {type(exc).__name__}: {exc}"[:500],
                               "referee": {"verdict": "could_not_run"}}
                        with a.out.open("a") as f:
                            f.write(json.dumps(rec) + "\n")
                    print(f"  -> {rec.get('referee', {}).get('verdict')} in {rec.get('wall_s')} s", flush=True)
                    if rec.get("gw_quota_errors"):  # a provider's auth or quota refusal: stop, never retry in a loop
                        print(f"[{datetime.now():%H:%M}] stopping: the provider refused "
                              f"({rec['gw_quota_errors']} auth/quota errors)", flush=True)
                        return
    finally:
        if gpu and MARKER.exists() and MARKER.read_text().startswith(SESSION + " "):
            MARKER.unlink()


if __name__ == "__main__":
    main()
