"""T2: the eight gateway-chain checks (adc-research 2026-10-26-open-gateway.md §4) against agentgateway.

Starts its own gateway (atolye-t2-gw) and a recording fake upstream (atolye-t2-echo), runs the checks, prints one
JSON result per check, and removes everything it started. Uses the host's vLLM for checks 2 and 7 (short requests).
Run: uv run python bench/t2/run_checks.py
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE.parents[1] / "runs" / "t2"
GW_IMAGE = "cr.agentgateway.dev/agentgateway:v1.6.0"
PY_IMAGE = "ghcr.io/openhands/agent-server:1.58.2-python"
# Private networks: T2 never joins production's atolye-internal, so live sandboxes cannot reach the test gateway.
NET, INTERNAL = "atolye-t2", "atolye-t2-int"
GW, ECHO, FRONT, PORT = "atolye-t2-gw", "atolye-t2-echo", "atolye-t2-front", 14100
# The chain under test: client -> front filter -> agentgateway -> upstream. Set T2_DIRECT_GW=1 to test agentgateway alone.
DIRECT_GW = os.environ.get("T2_DIRECT_GW") == "1"
URL = f"http://127.0.0.1:{PORT}/v1/chat/completions"
KEY_ALL, KEY_ECHO_ONLY, KEY_LATE = "vk-t2-all-5d1c", "vk-t2-echo-9a7e", "vk-t2-late-3b2f"
UPSTREAM_SECRET = "UPSTREAM-SECRET-0123456789"


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, check=check)


def write_config(keys: list[dict]) -> None:
    (STATE / "config.yaml").write_text((HERE / "config.yaml").read_text().replace("KEYS", json.dumps(keys)))


def key(k: str, models: list[str], job: str) -> dict:
    return {"key": k, "metadata": {"job": job}, "allowedModels": models}


BASE_KEYS = [key(KEY_ALL, ["qwen3.8-27b", "echo", "echo-redirect"], "t2-all"), key(KEY_ECHO_ONLY, ["echo"], "t2-echo")]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # the client must see what the gateway returned, not follow it
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def post(body: dict, k: str = KEY_ALL, headers: dict | None = None, url: str = URL) -> tuple[int, str, dict]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {k}", "Content-Type": "application/json",
                                          **(headers or {})})
    try:
        with _OPENER.open(req, timeout=120) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def upstream_requests() -> list[dict]:
    p = STATE / "echo" / "requests.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def gw_log_lines() -> list[dict]:
    out = sh("podman", "logs", GW, check=False)
    recs = []
    for line in (out.stdout + out.stderr).splitlines():
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return [r for r in recs if "http.status" in r]


def setup() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "echo").mkdir(exist_ok=True)
    (STATE / "data").mkdir(exist_ok=True)
    (STATE / "echo" / "requests.jsonl").write_text("")
    if sh("podman", "network", "exists", NET, check=False).returncode:
        sh("podman", "network", "create", NET)
    if sh("podman", "network", "exists", INTERNAL, check=False).returncode:
        sh("podman", "network", "create", "--internal", INTERNAL)
    write_config(BASE_KEYS)
    uid, gid = sh("id", "-u").stdout.strip(), sh("id", "-g").stdout.strip()
    sh("podman", "run", "-d", "--name", ECHO, "--network", NET, "--userns=keep-id", "--user", f"{uid}:{gid}",
       "-v", f"{HERE / 'echo_upstream.py'}:/echo.py:ro,Z", "-v", f"{STATE / 'echo'}:/log:Z",
       "--entrypoint", "python3", PY_IMAGE, "/echo.py")
    gw_ports = ["-p", f"127.0.0.1:{PORT}:4000"] if DIRECT_GW else []
    sh("podman", "run", "-d", "--name", GW, "--network", NET, "--userns=keep-id",
       "--user", f"{uid}:{gid}", *gw_ports,
       "-v", f"{STATE / 'config.yaml'}:/config.yaml:Z", "-v", f"{STATE / 'data'}:/data:Z", GW_IMAGE, "-f", "/config.yaml")
    if not DIRECT_GW:
        (STATE / "redact.txt").write_text(UPSTREAM_SECRET + "\n")
        sh("podman", "run", "-d", "--name", FRONT, "--network", NET, "--userns=keep-id", "--user", f"{uid}:{gid}",
           "-p", f"127.0.0.1:{PORT}:4000", "-e", f"UPSTREAM=http://{GW}:4000", "-e", "SECRETS_FILE=/redact.txt",
           "-v", f"{STATE / 'redact.txt'}:/redact.txt:ro,Z",
           "-v", f"{HERE.parents[1] / 'stack' / 'front' / 'front_filter.py'}:/front_filter.py:ro,Z",
           "--entrypoint", "python3", PY_IMAGE, "/front_filter.py")
    for _ in range(60):
        try:
            if post({"model": "echo", "messages": [{"role": "user", "content": "ping"}]})[0] == 200:
                return
        except OSError:  # not listening yet, or exited on a config error (shown in the raise below)
            pass
        time.sleep(0.5)
    raise RuntimeError("t2 gateway not ready: " + sh("podman", "logs", GW, check=False).stderr[-500:])


def teardown() -> None:
    for name in (FRONT, GW, ECHO):  # one call each: podman refuses the whole batch if one name is missing
        sh("podman", "rm", "-f", "-t", "3", name, check=False)
    for net in (NET, INTERNAL):
        sh("podman", "network", "rm", net, check=False)


def check1_cache_control() -> dict:
    body = {"model": "echo", "messages": [
        {"role": "system", "content": [{"type": "text", "text": "stable prefix", "cache_control": {"type": "ephemeral"}}]},
        {"role": "user", "content": "hi"}]}
    status, _, _ = post(body)
    sent = json.loads(upstream_requests()[-1]["body"])
    kept = "cache_control" in json.dumps(sent.get("messages", []))
    return {"check": 1, "name": "cache_control survives the gateway", "pass": status == 200 and kept,
            "status": status, "upstream_messages": sent.get("messages")}


def check2_usage() -> dict:
    before = len(gw_log_lines())
    post({"model": "echo", "messages": [{"role": "user", "content": "hi"}]})
    status, text, _ = post({"model": "qwen3.8-27b", "stream": True, "stream_options": {"include_usage": True},
                            "max_tokens": 16, "messages": [{"role": "user", "content": "Count to three."}],
                            "chat_template_kwargs": {"enable_thinking": False}})
    time.sleep(1)
    new = gw_log_lines()[before:]
    echo_rec = next((r for r in new if r.get("gen_ai.request.model") == "pinned-echo-model"), {})
    stream_rec = next((r for r in new if "Qwen" in str(r.get("gen_ai.request.model", ""))), {})
    ok = (int(echo_rec.get("cached_input_tokens") or 0) == 80 and int(stream_rec.get("gen_ai.usage.input_tokens") or 0) > 0
          and int(stream_rec.get("gen_ai.usage.output_tokens") or 0) > 0 and stream_rec.get("job") == "t2-all")
    return {"check": 2, "name": "usage counted per job, cache field and streamed call", "pass": ok, "stream_status": status,
            "echo_log": {k: echo_rec.get(k) for k in ("job", "gen_ai.usage.input_tokens", "cached_input_tokens")},
            "stream_log": {k: stream_rec.get(k) for k in ("job", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens")}}


def check3_headers_and_secret() -> dict:
    hostile = {"x-session-id": "sandbox-chosen", "X-OpenRouter-Title": "sandbox", "X-Evil": "1", "Cookie": "c=1"}
    post({"model": "echo", "messages": [{"role": "user", "content": "hi"}]}, headers=hostile)
    got = {k.lower(): v for k, v in upstream_requests()[-1]["headers"].items()}
    leaked_vk = any(KEY_ALL in v for v in got.values())
    forwarded = sorted(k for k in (h.lower() for h in hostile) if k in got)
    upstream_auth_ok = got.get("authorization") == f"Bearer {UPSTREAM_SECRET}"
    status, text, resp_headers = post({"model": "echo", "messages": [{"role": "user", "content": "ECHO_AUTH"}]})
    echoed = UPSTREAM_SECRET in text or any(UPSTREAM_SECRET in v for v in resp_headers.values())
    return {"check": 3, "name": "no inbound header forwarding; secret never reaches the client",
            "pass": not leaked_vk and not forwarded and upstream_auth_ok and not echoed,
            "virtual_key_reached_upstream": leaked_vk, "client_headers_forwarded": forwarded,
            "provider_key_injected": upstream_auth_ok, "provider_key_echoed_back_to_client": echoed}


def check4_redirect_and_egress() -> dict:
    status, text, headers = post({"model": "echo-redirect", "messages": [{"role": "user", "content": "hi"}]})
    followed = status == 200
    egress = sh("podman", "run", "--rm", "--network", INTERNAL, "--entrypoint", "curl", PY_IMAGE,
                "-s", "-m", "5", "-o", "/dev/null", "-w", "%{http_code}", "https://pypi.org/", check=False).stdout
    return {"check": 4, "name": "upstream redirect not followed; sandbox has no egress",
            "pass": not followed and egress.strip() == "000", "redirect_status_to_client": status,
            "location_header_to_client": headers.get("Location") or headers.get("location"),
            "sandbox_internet_http_code": egress.strip()}


def check5_model_pin() -> dict:
    other, _, _ = post({"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
    raw, _, _ = post({"model": "RadixArk/Qwen3.8-27B-NVFP4", "messages": [{"role": "user", "content": "hi"}]})
    denied, _, _ = post({"model": "qwen3.8-27b", "messages": [{"role": "user", "content": "hi"}]}, k=KEY_ECHO_ONLY)
    post({"model": "echo", "messages": [{"role": "user", "content": "hi"}]}, k=KEY_ECHO_ONLY)
    upstream_model = json.loads(upstream_requests()[-1]["body"]).get("model")
    return {"check": 5, "name": "model pinned; others refused", "pass": other >= 400 and raw >= 400 and denied == 403
            and upstream_model == "pinned-echo-model", "unknown_model": other, "raw_upstream_name": raw,
            "key_not_allowed": denied, "model_sent_upstream": upstream_model}


def check6_key_lifecycle() -> dict:
    write_config(BASE_KEYS + [key(KEY_LATE, ["echo"], "t2-late")])
    t0 = time.monotonic()
    while post({"model": "echo", "messages": [{"role": "user", "content": "hi"}]}, k=KEY_LATE)[0] != 200:
        if time.monotonic() - t0 > 30:
            break
        time.sleep(0.1)
    accept_s = time.monotonic() - t0
    write_config(BASE_KEYS)
    t1 = time.monotonic()
    while post({"model": "echo", "messages": [{"role": "user", "content": "hi"}]}, k=KEY_LATE)[0] != 401:
        if time.monotonic() - t1 > 30:
            break
        time.sleep(0.1)
    revoke_s = time.monotonic() - t1
    return {"check": 6, "name": "per-job key: usable fast, revoked fast", "pass": accept_s < 5 and revoke_s < 5,
            "seconds_to_accept": round(accept_s, 2), "seconds_to_revoke": round(revoke_s, 2)}


def check7_overhead(n: int = 8) -> dict:
    body = {"max_tokens": 64, "messages": [{"role": "user", "content": "Write one sentence about rivers."}],
            "chat_template_kwargs": {"enable_thinking": False}}

    def one(via_gw: bool) -> float:
        t = time.monotonic()
        if via_gw:
            post({**body, "model": "qwen3.8-27b"})
        else:
            post({**body, "model": "RadixArk/Qwen3.8-27B-NVFP4"}, url="http://127.0.0.1:8000/v1/chat/completions")
        return time.monotonic() - t

    res = {}
    for via in (False, True):
        with ThreadPoolExecutor(n) as ex:
            res["gateway" if via else "direct"] = sorted(ex.map(lambda _: one(via), range(n)))
    d, g = statistics.median(res["direct"]), statistics.median(res["gateway"])
    return {"check": 7, "name": f"overhead at {n} concurrent requests", "pass": g - d < 0.25,
            "median_direct_s": round(d, 3), "median_gateway_s": round(g, 3)}


def check8_arm64() -> dict:
    arch = sh("podman", "image", "inspect", GW_IMAGE, "--format", "{{.Architecture}}").stdout.strip()
    return {"check": 8, "name": "runs on arm64", "pass": arch == "arm64", "arch": arch}


def check9_malformed_requests() -> dict:
    """The front filter refuses targets and framings a sandbox could use to reach other upstream endpoints."""
    import http.client

    if DIRECT_GW:
        return {"check": 9, "name": "malformed requests refused (front filter only)", "pass": True, "skipped": True}
    results = {}
    for target in ("/v1/chat/completions/x", "/v1/modelsX", "/v1/responses/../../files", "/v1/models?x=1",
                   "http://evil/v1/chat/completions", "/v1/%2e%2e/admin"):
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        c.putrequest("POST", target, skip_host=True)
        c.putheader("Host", "x")
        c.putheader("Authorization", f"Bearer {KEY_ALL}")
        c.putheader("Content-Length", "2")
        c.endheaders(b"{}")
        results[target] = c.getresponse().status
        c.close()
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    c.putrequest("POST", "/v1/chat/completions", skip_host=True)
    c.putheader("Host", "x")
    c.putheader("Content-Length", "-1")
    c.endheaders()
    results["content-length -1"] = c.getresponse().status
    c.close()
    return {"check": 9, "name": "malformed requests refused", "pass": all(s >= 400 for s in results.values()),
            "statuses": results}


def main() -> None:
    teardown()
    setup()
    try:
        for fn in (check1_cache_control, check2_usage, check3_headers_and_secret, check4_redirect_and_egress,
                   check5_model_pin, check6_key_lifecycle, check7_overhead, check8_arm64,
                   check9_malformed_requests):
            try:
                print(json.dumps(fn()), flush=True)
            except Exception as exc:  # a crashed check is a failed check, reported with its cause
                print(json.dumps({"check": fn.__name__, "pass": False, "error": repr(exc)}), flush=True)
    finally:
        teardown()


if __name__ == "__main__":
    main()
