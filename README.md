# atolye (working name)

An open-source, OpenHands-centred stack for unattended software development, with **small, local or free models**
first and **verifiable results**.

**Status:** pre-alpha. The first slice is the harness for a three-way comparison (see below). Nothing here is
production-ready.

## What it is

We do not write another agent loop. We put the parts that unattended work needs **around** OpenHands (MIT), and
contribute generic fixes upstream:

| Layer | Component |
|---|---|
| Agent loop, tools, sandbox | [OpenHands Software Agent SDK + Agent Server](https://github.com/OpenHands/software-agent-sdk), pinned version, rootless Podman |
| Model access | A small open gateway: per-job keys, no provider key in the sandbox, stream and cache usage counted. Optionally chained to LLMDAP (commercial edition) |
| Verification | A language-agnostic referee: hidden acceptance tests on a clean image, with an integrity guard |
| Memory | [Kütüphane](https://github.com/ekaynac/kutuphane) over MCP (governed, versioned, citable) |
| Orchestration / delivery | MCP tools such as nöbetçi-mcp (shared contract; separate project) |

## Profiles

- **on-prem:** local models only (vLLM/Ollama), air-gapped.
- **standard:** subscriptions, local models, and no-training API routes.
- **open:** any reachable model, for example through OpenRouter.

## The first slice: the three-way comparison harness

`runner/` runs one item headless:
1. base checkout;
2. OpenHands Agent Server in a rootless container;
3. the local model;
4. patch;
5. referee;
6. one metrics JSON line.

The same items and the same referee are used for:
- ADC's local run;
- an orchestrator that uses nöbetçi-mcp;
- this stack.

## Licence

Apache-2.0. See `LICENSE`.
