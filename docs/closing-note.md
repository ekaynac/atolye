# atolye: closing note (2026-11-04)

atolye is **closed and archived**. This note records what was built, what was measured, and why it stopped. The
README lists what can be reused.

## 1. Why it existed

On 2026-10-26 the operator stopped work on ekaynac/auto-dev-cycle (ADC), the team's own unattended-development
framework. In its place he asked for an OpenHands-centred stack: OpenHands as the agent loop, Kütüphane as governed
memory, LLMDAP as the commercial auth and audit layer, and a small open gateway. Local models came first.

atolye was to add only what unattended work needs and OpenHands lacks:
- keys outside the sandbox;
- a referee with hidden acceptance tests;
- governed memory;
- cost per accepted outcome.

Its first job was to compare three systems on the same items: ADC, an orchestrator on nöbetçi-mcp, and this stack.

## 2. Why it stopped

The operator's judgement, 2026-11-04: a capable orchestrator model with a cloud builder does this work better, and
deciding the scope of a job needs a capable model anyway. The evidence below supports him:

1. **The local model solves small jobs only.**
   - The test: three private product tasks, rebuilt from their start commits with the tests merged with each task as
     hidden acceptance, run with mini-swe-agent, 100 steps, 3 repeats each.
   - Qwen3.8-27B: the small task (3 files) 3/3; the two medium tasks (30–35 files) 0/3 each.
   - Qwen3.6-35B-A3B: the same. Its context was 64k, against 262k for Qwen3.8, so its medium-task runs are not a fair
     test.
   - The kill criterion written before the run (at least 2 of 3 tasks accepted) failed.
2. **Routing small jobs to the local model needs a model to judge job size.**
   - A cheap decider did not work: the zero-shot Qwen3.8-27B decider auto-decided 0 % of review rounds at any accuracy
     bar from 80 % to 95 % (AUC 0.55; adc-dev's T4).
   - OpenRouter's Auto Router decides per request, by task type. It reaches a self-hosted model only through an
     Enterprise add-on, over public HTTPS, and our private code would pass through OpenRouter.
3. **The saving is small next to the cost of running the local line.**
   - nöbetçi's supervisor cost about $2 per goal at list price.
   - The local line needed GPU windows shared with a training job, and caused memory incidents (§4).
4. **There is no outside user.** No pilot site was asked for or found. Nothing in the plan built the intake and
   lifecycle layer, which is what a product needs first.

## 3. What was measured

| What | Result | Where |
|---|---|---|
| **Gateway chain** (T2) | front filter + agentgateway v1.6.0 9/9 checks; agentgateway alone 7/8 (no header allowlist, no redaction of an echoed key) | `docs/gateway.md`, `bench/t2/` |
| **Local workers** (stage 2) | 10 items, 2 repeats, Qwen3.8-27B NVFP4 on vLLM. **mini-swe-agent 16/20, OpenHands 14/20.** Per accepted result: mini 0.46M input tokens and 4.5 GPU-min; OpenHands 1.05M and 6.4. 1 of 20 mini runs touched a path outside the editable list, against 6 of 20 for OpenHands. ADC on 3 of the items: 2/3 at 90 min and 3.64M tokens per accepted result | research workspace, three-way report §2–§3 |
| **Main failure cause** | 6 of 10 rejections hit the 50-turn cap. At 100 turns, 4 of the 5 cap-bound cells passed (1 sample each) | three-way report §4b |
| **OpenHands text-only stops** (`software-agent-sdk#5510`) | 3/20 local, 5/18 cloud, against 1/36 in adc-dev's harness. Confounded: atolye's task lacked the harness's "When you are done, call the finish tool." line (fixed in PR #17, not re-measured) | three-way report §4a |
| **Quality** (blind review of 43 accepted diffs) | No arm clearly better. The main gap in every group was tests, then duplication, then leftover files | blind review |
| **Memory** (T3, Kütüphane over MCP, headless) | It works with no human: 18/18 runs. It did not help: 8/9 against 9/9 accepted, and +50 % input tokens, because every search returned full passages. Kütüphane added snippets (kutuphane#121): the same 25 searches returned 76 % less text. The rerun was not done: gemma4-31b made no memory calls, and the host ran out of memory | T3 report |
| **Private product tasks** | see §2, item 1 | three-way report §8 |
| **vLLM on the DGX Spark** (GB10) | 0.26 → 0.33: decode 54.8 → 56.2 tok/s, first token on a 60k prompt 39.5 → 35.1 s, 4 streams 162.7 → 157.5 tok/s. Humming 0.1.18 crashes on GB10: the memory clock reads 0, which leads to a division by zero | research workspace `ops/` |

## 4. What went wrong on the shared host (lessons)

1. **2026-10-27: 18 runs shared the GPU with a training job.** The training started without a marker. Since then the
   GPU guard reads `nvidia-smi` and refuses to start while a non-inference process is on the GPU.
2. **2026-10-31: the host ran out of memory, and the kernel killed other users' services.** atolye's sandboxes ran
   beside the training and vLLM. Since then no cell starts below 16 GiB of available memory.
3. **2026-11-02 15:03: production vLLM was down for 8 minutes.** A side-by-side test instance held memory, and the
   production restart failed. Since then the benchmark runs one vLLM at a time.
4. **2026-11-02 20:31: the kernel killed another session's agent process.** A timer started the training while vLLM
   was in its large mode, and the mode watcher's 60 s graceful stop was too slow. Since then the watcher kills vLLM at
   once when a training process appears, and shrinks it 30 min before a training timer is due.
5. **Three referee runs in parallel timed out** on a large task. Since then the referee runs one at a time.
6. **A prompt handover dropped one line.** Every OpenHands comparison with the other harness was confounded by it.
   The lesson: compare the exact text sent, byte for byte, before comparing results.

## 5. State at closing

- The repository is archived. All 25 PRs are merged or closed.
- The private items, run records and patches stay on the host, outside the repository.
- The research workspace keeps the reports, notes and the host-specific closing details.
- Nothing of atolye keeps running. The production vLLM configuration it set up (0.33, small/large mode watcher) stays
  as the operator decided. Its files are outside this repository.
