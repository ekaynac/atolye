"""Prefix-cache use on the local vLLM, per run.

vLLM's responses carry no cached-token count (`prompt_tokens_details` is null), but its Prometheus counters do. The
counters are server-wide, so a run's delta is only that run's while it is the server's only user. The record says
whether that held: the delta of prompt tokens must equal what the gateway counted for the job.
"""

from __future__ import annotations

import urllib.request

METRICS_URL = "http://127.0.0.1:8000/metrics"
COUNTERS = {"vllm:prompt_tokens_total": "prompt", "vllm:prefix_cache_hits_total": "cache_hit",
            "vllm:generation_tokens_total": "generation"}


def snapshot() -> dict[str, float] | None:
    try:
        with urllib.request.urlopen(METRICS_URL, timeout=5) as r:
            text = r.read().decode()
    except OSError:
        return None
    out = {v: 0.0 for v in COUNTERS.values()}
    for line in text.splitlines():
        name = line.split("{", 1)[0]
        if name in COUNTERS:
            out[COUNTERS[name]] += float(line.rsplit(" ", 1)[1])
    return out


def delta(before: dict[str, float] | None, after: dict[str, float] | None, gw_input_tokens: int) -> dict:
    if not before or not after:
        return {"vllm_metrics": "unavailable"}
    d = {k: int(after[k] - before[k]) for k in before}
    return {
        "vllm_prompt_tokens": d["prompt"],
        "vllm_cache_hit_tokens": d["cache_hit"],
        "vllm_generation_tokens": d["generation"],
        # Exclusive: nobody else used vLLM during the run, so the cache figures are this job's alone.
        "vllm_exclusive": d["prompt"] == gw_input_tokens,
    }
