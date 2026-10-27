"""Summarise run records (JSON lines) per cell: arm x item x model.

Usage: atolye-report runs/a.jsonl [runs/b.jsonl ...]
Prints a Markdown table: runs, accepted, median and range of wall time, LLM calls, input and output tokens, the vLLM
prefix-cache share (only from exclusive runs), and the stop causes. Cost per accepted outcome is reported in its
parts (wall minutes on the local GPU, input tokens), never summed into one figure.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path


def _rng(vals: list[float]) -> str:
    if not vals:
        return "-"
    med = statistics.median(vals)
    return f"{med:,.0f} ({min(vals):,.0f}-{max(vals):,.0f})" if len(vals) > 1 else f"{med:,.0f}"


def _stop(rec: dict) -> str:
    s = str(rec.get("stop", "?"))
    if "MaxIterations" in s:
        return "max_iterations"
    if s.startswith("error"):
        return "error"
    return s.rsplit(".", 1)[-1].lower()


def summarise(records: list[dict]) -> str:
    cells: dict[tuple, list[dict]] = defaultdict(list)
    for r in records:
        cells[(r.get("arm", "?"), r.get("item", "?"), r.get("model", "?"))].append(r)
    rows = ["| arm | item | model | runs | accepted | wall s | LLM calls | input tok | output tok | cache hit % "
            "| stops | wall min per accepted |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for (arm, item, model), rs in sorted(cells.items()):
        acc = sum(1 for r in rs if (r.get("referee") or {}).get("verdict") == "accepted")
        excl = [r for r in rs if r.get("vllm_exclusive") and r.get("vllm_prompt_tokens")]
        cache = (f"{100 * sum(r['vllm_cache_hit_tokens'] for r in excl) / sum(r['vllm_prompt_tokens'] for r in excl):.0f}"
                 if excl else "-")
        per_accepted = f"{sum(r.get('wall_s', 0) for r in rs) / 60 / acc:.1f}" if acc else "-"
        stops = ", ".join(f"{k} {v}" for k, v in Counter(_stop(r) for r in rs).most_common())
        tok_in = [r.get("gw_input_tokens", r.get("prompt_tokens", 0)) for r in rs]
        tok_out = [r.get("gw_output_tokens", r.get("completion_tokens", 0)) for r in rs]
        rows.append(f"| {arm} | {item} | {model} | {len(rs)} | {acc}/{len(rs)} | {_rng([r.get('wall_s', 0) for r in rs])} "
                    f"| {_rng([r.get('llm_calls', 0) for r in rs])} | {_rng(tok_in)} | {_rng(tok_out)} | {cache} "
                    f"| {stops} | {per_accepted} |")
    return "\n".join(rows)


def main() -> None:
    records = [json.loads(line) for p in sys.argv[1:] for line in Path(p).read_text().splitlines() if line.strip()]
    print(summarise(records))


if __name__ == "__main__":
    main()
