# Plan

**Source of the decisions:** `/home/mega/adc-research/DECISIONS.md` (R13–R26) and the research in that directory.

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
| `atolye.run_item`: item → checkout → OpenHands headless → patch → referee → one JSON line | done, smoke-tested |
| `atolye.referee`: **stand-in** (clean checkout, protected-path refusal, hidden files, no network) | done; replaced by P-A (language-agnostic `adc dev referee`) for measured runs |
| Gateway in front of the model (per-job key, usage counting) | next: T2 with agentgateway |
| Kütüphane memory over MCP (headless token) | after `ekaynac/kutuphane#73` |
| Item sets G (Go) and P (Python/TypeScript, public repos) with hidden acceptance | next |

## Known gaps

- **Cache-read tokens are not visible** from vLLM responses (`prompt_tokens_details` is null). Read vLLM's
  `/metrics` prefix-cache counters per run, or enable prompt-token details on the server (that is the server
  owner's decision).
- **The model call goes straight from the container to vLLM.** There is no key on the local route, but there is no
  per-job accounting either until the gateway exists.
