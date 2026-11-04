# atolye (archived)

**Status: closed on 2026-11-04 and archived.** This repository is kept for reference. Read
[`docs/closing-note.md`](docs/closing-note.md) first: why it existed, what it measured, and why it stopped.

atolye was a harness for running coding agents unattended and measuring them honestly. Its focus was local models on
one GPU box (an NVIDIA DGX Spark, arm64). It did not write its own agent loop. It wrapped existing workers in four
parts:
- a sandbox with no network;
- a model gateway that keeps provider keys out of the sandbox and counts tokens per job;
- a referee that judges every patch against hidden tests the agent never sees;
- optional governed memory ([Kütüphane](https://github.com/ekaynac/kutuphane)) over MCP.

## What it found, in one table

| Question | Answer |
|---|---|
| Can a local 27B model do real coding jobs unattended? | **Small ones.** It solved 16/20 small benchmark items and 3/3 runs of a small private product task. It solved 0/6 runs of two medium product tasks (30–35 files). Every miss hit the step cap |
| mini-swe-agent or OpenHands as the local worker? | **mini-swe-agent:** 16/20 against 14/20 accepted, less than half the tokens per accepted result, and fewer edits outside the allowed paths. OpenHands only when MCP tools, a browser or its Agent Server API are needed |
| Does the front filter add anything to agentgateway? | **Yes:** 9/9 checks with it, 7/8 without |
| Does governed memory help an agent here? | **Not shown.** It works headless, but cost +50 % tokens with no gain on these items. Kütüphane then added snippets, which cut the search text by 76 % on a replay |
| Is routing small jobs to a local model worth it? | **Not for this team:** a capable model must judge job size anyway, and the saving is small. See the closing note §2 |

The full results are in the closing note §3 and in the research workspace reports it cites.

## What can be reused

Each part is small and stands alone. Apache-2.0.

| Part | Path | What it does | State |
|---|---|---|---|
| **Front filter** | `stack/front/front_filter.py`, `tests/test_front_filter.py` | Stdlib-Python reverse proxy in front of a model gateway. Allowlist of methods, paths and request headers; allowlist of response headers; streaming-safe redaction of provider secrets, raw and JSON-escaped | Tested (T2 9/9, a secret split at every chunk boundary) |
| **Gateway wiring** | `src/atolye/gateway.py`, `stack/gateway/*.yaml`, `docs/gateway.md` | agentgateway v1.6.0 in rootless Podman behind the front filter. Sandboxes reach only the filter (internal network). Per-job virtual keys with expiry; usage per job from the access log; cloud routes rendered only while their key file exists; 401/402/403/429 counted per job | Used for every run |
| **Agent Server in rootless Podman** | `src/atolye/podman.py`, `stack/agent-image/Containerfile` | OpenHands Agent Server 1.58.2 in a derived image of the item's own image, `--userns=keep-id`, no-new-privileges, pids and memory limits, loopback port. Host-side git uses a separate bare repository, so a hostile `.git` in the sandbox runs nothing on the host | Checked against a hostile `.git` |
| **mini-swe-agent worker** | `src/atolye/mini_worker.py`, `stack/mini/atolye.yaml` | mini-swe-agent 2.4.6, with the loop on the host and commands in a `--network none` container. `HOME` stays outside the work tree. atolye's caps override the config's. The config asks the agent to add tests | Used for every mini run |
| **Runner and referee wrapper** | `src/atolye/run_item.py`, `src/atolye/referee.py` | One item: checkout, item setup (with network), agent (without), patch against base, then P-A, the referee (`adc dev referee` from ekaynac/auto-dev-cycle, frozen). `could_not_run` is judged again. One JSON line per run | Used for every run |
| **Campaigns** | `src/atolye/campaign.py` | Cells (item × worker × repeat) in repeat-major order, `--parallel N`, resume at cell boundaries, `--until HH:MM`. It holds a GPU marker file, and starts no cell while a non-inference process is on the GPU or under 16 GiB of free memory. A provider refusal stops new cells | Used for every campaign |
| **Report** | `src/atolye/report.py` | Markdown table per arm × item × model: acceptance, wall time, calls, tokens, cache share (exclusive runs only), stop causes, edits outside the allowed paths, memory calls | — |
| **Item format** | `items/README.md` | A directory with `item.json` (a P-A item: repository, base, image, setup, test command, hidden acceptance, editable and protected paths) and `requirement.md`, the only text the agent receives. Check each item two-sided: the reference patch is accepted and an empty patch is rejected | 4 public-repository items (set P); their prepared images are in `items/p/` |
| **T2 checks** | `bench/t2/` | Nine checks that a model gateway keeps a sandbox from steering or leaking: headers, redaction, model pin, malformed requests | — |

**Not in this repository:** the hidden tests and prompts of the benchmark items, the private product tasks, and the
run records. They stay private on the host where they ran.

## Running it (for reference)

```sh
uv sync --extra mini                       # Python 3.12; mini-swe-agent is optional
export ATOLYE_ACCEPTANCE=/path/to/items    # a directory of item dirs (see items/README.md)
uv run atolye-run $ATOLYE_ACCEPTANCE/P/<id> --out runs/one.jsonl                       # one item, default worker mini
uv run atolye-campaign --items <dir> [<dir> ...] --workers mini openhands --repeats 2 --parallel 2 --out runs/c.jsonl
uv run atolye-report runs/c.jsonl
```

Requirements:
- rootless Podman;
- an OpenAI-compatible model server (default: a local vLLM on `:8000`, served as `qwen3.8-27b` through the gateway;
  see `stack/gateway/config.yaml`);
- the P-A referee binary (`$ATOLYE_REFEREE`).

Defaults: 100 turns and 60 min per item.

The GPU marker, the stop file and the memory floor are tuned for one shared host: see the paths at the top of
`src/atolye/campaign.py`.

## Where the research lives

The decisions (R1–R36), the reports (stage 2, the blind review, T3, the critique, the DeepSeek super-agent
comparison) and the host operations notes are in the research workspace that drove this repository. It is not
published. The closing note quotes the numbers that matter.

## Licence

Apache-2.0. See `LICENSE`. `stack/mini/atolye.yaml` is derived from mini-swe-agent's `swebench.yaml` (MIT); see its
header.
