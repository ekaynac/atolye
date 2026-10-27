# Plan

**Source of the decisions:** `/home/mega/adc-research/DECISIONS.md` (R13–R30) and the research in that directory.

## Principles

- **Do not write an agent loop.** Use OpenHands, with pinned versions and few local patches. Each patch gets an
  upstream PR (upstream-first).
- **Build only what unattended work needs and OpenHands lacks:**
  - keys out of the sandbox;
  - a referee with hidden acceptance;
  - governed memory;
  - cost per accepted outcome.
- **Local models first:** the `on-prem` profile must work with mid-size open models on one GPU box.

## Slice 1: the comparison harness (arm C of the three-way comparison)

| Part | State |
|---|---|
| `atolye.podman`: Agent Server in rootless Podman (`--userns=keep-id`, loopback port, no-new-privileges, pids and memory limits); derived agent image per item image (`stack/agent-image/Containerfile`); the item's `setup` step before the agent | done; offline `go test` checked in the sandbox |
| `atolye.run_item`: item dir (P-A `item.json` + `requirement.md`) → checkout → setup → agent → patch → P-A → one JSON line | done |
| Host-side git never touches the sandbox's `.git` (separate bare repository) | done, checked against a hostile `.git` (fsmonitor, filter, textconv: none ran) |
| `atolye.referee`: **P-A** (`adc dev referee`, auto-dev-cycle `9ee8a40a`), the one referee for every arm; `could_not_run` is judged again | done; set G reference accepted / empty rejected through the wrapper |
| Front filter + agentgateway (per-job keys, usage per job; `docs/gateway.md`) | done, T2 9/9 |
| `--worker mini` (mini-swe-agent, T1's second worker) | runs; trajectories saved |
| `atolye-campaign`: cells one at a time, `GPU-NOW` marker, resume at cell boundaries | done |
| vLLM prefix-cache deltas per run; `atolye-report` | done |
| Set P in P-A format (private; two-sided with P-A) | in progress |
| Set G (Go; adc-dev's items, private copy) | ready |
| Kütüphane memory over MCP (headless token) | after `ekaynac/kutuphane#73` |

## Caps (stage-2 coordination, 2026-10-27)

- 50 turns (the same as adc-dev's T1-cloud cells) and 60 min wall per item.
- Parallel 1, the same as arm A.
- The agent gets the item's `env K=V` pairs and no network.

## Known gaps

- **vLLM's counters are server-wide.** A run's cache share counts only when it was the server's only user
  (`vllm_exclusive`). The GPU marker (one session at a time) is what makes that hold.
