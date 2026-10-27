"""A fake OpenAI-compatible upstream for gateway checks: records what it received, answers like a chat model.

POST /v1/chat/completions -> records headers+body to /log/requests.jsonl, returns a fixed completion with usage.
  A body containing ECHO_AUTH makes it act hostile: it returns the Authorization header it received.
POST /redirect/v1/chat/completions -> 302 to http://example.invalid/ (a gateway must not follow it).
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 (http.server API)
        body = self.rfile.read(int(self.headers.get("content-length", 0))).decode("utf-8", "replace")
        with open("/log/requests.jsonl", "a") as f:
            f.write(json.dumps({"path": self.path, "headers": dict(self.headers), "body": body}) + "\n")
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "http://example.invalid/steal")
            self.end_headers()
            return
        content = self.headers.get("authorization", "") if "ECHO_AUTH" in body else "ok"
        out = json.dumps({
            "id": "echo-1", "object": "chat.completion", "model": json.loads(body or "{}").get("model", ""),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 1, "total_tokens": 101,
                      "prompt_tokens_details": {"cached_tokens": 80}},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
