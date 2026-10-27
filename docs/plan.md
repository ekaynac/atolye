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
| `atolye.podman`: Agent Server in rootless Podman (`--userns=keep-id`, loopback port, no-new-privileges, pids and memory limits) | done, smoke-tested |
| `atolye.run_item`: item → checkout → agent → patch → referee → one JSON line | done, smoke-tested |
| Host-side git never touches the sandbox's `.git` (separate bare repository) | done, checked against a hostile `.git` (fsmonitor, filter, textconv: none ran) |
| `atolye.referee`: **stand-in** (allowed paths, protected files, read-only hidden tests, no network) | done; P-A (language-agnostic `adc dev referee`) replaces it for measured runs |
| Front filter + agentgateway (per-job keys, usage per job; `docs/gateway.md`) | done, T2 9/9 |
| `--worker mini` (mini-swe-agent, T1's comparison worker) | runs; first smoke produced no patch (40 steps): look at its trajectory on the next GPU slot |
| vLLM prefix-cache deltas per run; `atolye-report` | done |
| Set P (4 items, private hidden acceptance, fairness-reviewed) | done |
| Set G (Go) and a Go-capable agent image | waits for adc-dev's items |
| Kütüphane memory over MCP (headless token) | after `ekaynac/kutuphane#73` |

## Known gaps

- **vLLM's counters are server-wide.** A run's cache share counts only when it was the server's only user
  (`vllm_exclusive`). The GPU rule (one arm at a time) is what makes that hold.
- **The stand-in referee is not P-A.** Measured runs wait for P-A.
