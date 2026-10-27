# Model gateway: agentgateway v1.6.0, and what T2 found

**What runs:**
- `atolye-gw` (`cr.agentgateway.dev/agentgateway:v1.6.0`, arm64) sits on two networks: `podman` (to reach the
  host's vLLM) and `atolye-internal` (`--internal`: no egress).
- Agent sandboxes join only `atolye-internal`, so the gateway is the one service they can reach.
- Each job gets a virtual key. `atolye.gateway.job_key` adds it to the config file (file storage mode) and removes
  it when the job ends. The admin API stays on the container's loopback.
- Usage is read back per job from the JSON access log (`job`, input/output tokens, cached tokens).

## T2 results (2026-10-26, `bench/t2/run_checks.py`, raw output in `runs/t2-results.jsonl`)

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | `cache_control` survives the gateway | **pass** | the fake upstream received the content block with `cache_control` |
| 2 | Usage counted per job, including cache fields and streamed calls | **pass** | log: job `t2-all`, `cached_input_tokens` 80 (from the upstream's `prompt_tokens_details`); streamed vLLM call 16 in / 7 out |
| 3 | No inbound header forwarding; the provider key never reaches the client | **fail** | with a denylist, `cookie`, `x-session-id` and `x-openrouter-*` are removed, but an unknown header (`x-evil`) is still forwarded. A hostile upstream that echoes the `Authorization` it received gets the provider key back to the client unredacted |
| 4 | An upstream redirect is not followed; the sandbox has no egress | **pass** | the client received the `302` (the gateway did not fetch `example.invalid`); a container on `atolye-internal` gets no answer from `https://pypi.org` |
| 5 | The model is pinned; other models are refused | **pass** | unknown model 403, raw upstream name 403, key not allowed for the model 403; the upstream always receives the pinned name |
| 6 | Per-job key is usable and revocable quickly | **pass** | 0.31 s to accept, 0.31 s to revoke |
| 7 | Overhead at 8 concurrent requests | **pass** | median 2.89 s via the gateway vs 3.17 s direct: no measurable overhead at this size |
| 8 | Runs on arm64 | **pass** | image architecture arm64 |

The end-to-end smoke run through the gateway (`items/smoke-py.json`) recorded 10 requests, 80,977 in and 1,819 out.
That matches the SDK's own counts exactly. The hidden acceptance passed.

## What check 3 means, per profile

| Profile | Header forwarding | Provider-key echo |
|---|---|---|
| `on-prem` (local vLLM, no key on the route) | The sandbox can add headers the local server sees: low risk | Moot: there is no key to echo |
| `standard` / `open` (cloud keys) | The sandbox can steer an upstream with headers the denylist does not know | A buggy or hostile upstream could return the key to the sandbox |

## Options for the cloud profiles (not decided)

1. **A small front filter** in front of agentgateway (my estimate: 200–300 lines):
   - an inbound header **allowlist** (`content-type`, `accept`, the virtual key);
   - streaming-aware redaction of configured secrets in responses.

   It keeps agentgateway's keys, budgets, pricing and logs.
2. **Upstream:** feature requests to agentgateway for an allowlist mode on `requestHeaders`, and for response
   redaction of backend credentials. They would remove option 1 when they land.
3. **Extract ADC's gateway**, which meets check 3 already (`/home/mega/auto-dev-cycle` `internal/gateway`).
   Estimate: about 1.2–1.8k lines, and it lacks budgets, pricing and an Anthropic path.

**Known gap:** the gateway's stats port (15020) listens on all interfaces inside the container, so a sandbox on
`atolye-internal` can read aggregate Prometheus metrics. It exposes no prompts or keys, only counters by model.
