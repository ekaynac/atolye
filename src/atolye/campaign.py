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
import sys
from datetime import datetime
from pathlib import Path

from atolye.run_item import DEFAULT_MAX_ITERATIONS, DEFAULT_TIMEOUT_S, run

MARKER = Path("/home/mega/adc-research/GPU-NOW")
SESSION = "atolye"


def _done(out: Path) -> set[tuple[str, str, int]]:
    if not out.exists():
        return set()
    recs = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    return {(r["item"], r["worker"], int(r.get("repeat", 0))) for r in recs}


def main() -> None:
    p = argparse.ArgumentParser(prog="atolye-campaign")
    p.add_argument("--items", type=Path, nargs="+", required=True)
    p.add_argument("--workers", nargs="+", default=["openhands", "mini"], choices=("openhands", "mini"))
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--job", default="arm-C", help="the job name written into the marker")
    p.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    a = p.parse_args()
    a.out = a.out.resolve()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    if MARKER.exists() and not MARKER.read_text().startswith(SESSION + " "):
        sys.exit(f"the GPU is in use: {MARKER} says {MARKER.read_text().strip()!r}")
    MARKER.write_text(f"{SESSION} {a.job} {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    try:
        done = _done(a.out)
        for repeat in range(a.repeats):  # repeat-major: every cell once before any cell twice
            for item in a.items:
                for worker in a.workers:
                    if (item.name, worker, repeat) in done:
                        continue
                    print(f"[{datetime.now():%H:%M}] {item.name} {worker} repeat {repeat}", flush=True)
                    rec = run(item.resolve(), a.out, None, a.max_iterations, a.timeout, worker, repeat=repeat)
                    print(f"  -> {rec.get('referee', {}).get('verdict')} in {rec.get('wall_s')} s", flush=True)
    finally:
        if MARKER.exists() and MARKER.read_text().startswith(SESSION + " "):
            MARKER.unlink()


if __name__ == "__main__":
    main()
