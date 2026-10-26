"""atolye front filter: the only service a sandbox can reach. Sits in front of agentgateway.

Why: agentgateway forwards client headers it does not know (it has a denylist, not an allowlist) and does not redact
a provider credential that an upstream echoes back (bench/t2, check 3). This filter closes both, and nothing else:
- requests: only allowlisted methods, paths and headers are forwarded; the body is passed through unchanged;
- responses: only allowlisted headers are returned; every configured secret (raw and JSON-escaped) is replaced in the
  body, streaming-safe (a secret split across chunks is still caught).
Stdlib only, so it runs in any Python 3.10+ image. Configuration by environment:
  UPSTREAM (default http://atolye-gw:4000), SECRETS_FILE (one secret per line; optional), PORT (default 4000).
"""

from __future__ import annotations

import http.client
import json
import os
import select
import socket
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = urllib.parse.urlsplit(os.environ.get("UPSTREAM", "http://atolye-gw:4000"))
PORT = int(os.environ.get("PORT", "4000"))
MAX_BODY = 16 * 1024 * 1024
PATHS = ("/v1/chat/completions", "/v1/messages", "/v1/responses", "/v1/models", "/v1/embeddings")
REQUEST_HEADERS = ("authorization", "content-type", "accept")
RESPONSE_HEADERS = ("content-type", "retry-after")
REDACTED = b"[REDACTED]"


def secret_forms(secrets: list[str]) -> list[bytes]:
    """The byte forms to redact: each secret raw and JSON-escaped, longest first."""
    forms: set[bytes] = set()
    for s in (s.strip() for s in secrets):
        if len(s) >= 8:  # a shorter "secret" would redact ordinary text
            forms.add(s.encode())
            forms.add(json.dumps(s)[1:-1].encode())
    return sorted(forms, key=len, reverse=True)


def _load_secret_forms() -> list[bytes]:
    path = os.environ.get("SECRETS_FILE")
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return secret_forms(f.read().splitlines())


class Redactor:
    """Replaces secrets in a byte stream; holds back enough bytes that a secret split across chunks is still seen."""

    def __init__(self, forms: list[bytes]) -> None:
        self.forms = forms
        self.hold = max((len(f) for f in forms), default=1) - 1
        self.held = b""
        self.count = 0

    def _scrub(self, data: bytes) -> bytes:
        for form in self.forms:
            n = data.count(form)
            if n:
                self.count += n
                data = data.replace(form, REDACTED)
        return data

    def feed(self, chunk: bytes) -> bytes:
        data = self._scrub(self.held + chunk)
        if self.hold <= 0:
            self.held = b""
            return data
        self.held = data[-self.hold:]
        return data[:-self.hold]

    def flush(self) -> bytes:
        data, self.held = self._scrub(self.held), b""
        return data


_forms_cache: tuple[float, list[bytes]] = (-1.0, [])


def current_secret_forms() -> list[bytes]:
    """The secrets file is re-read when it changes, so a key added later is redacted without a restart."""
    global _forms_cache
    path = os.environ.get("SECRETS_FILE")
    mtime = os.stat(path).st_mtime if path and os.path.exists(path) else 0.0
    if mtime != _forms_cache[0]:
        _forms_cache = (mtime, _load_secret_forms())
    return _forms_cache[1]


def checked_path(target: str) -> str | None:
    """The exact allowed path for a request target, or None. Only origin-form targets, no query, no encoding tricks."""
    if not target.startswith("/") or any(s in target for s in ("%", "\\", "//", "/.", "?", "#")):
        return None
    return target if target in PATHS else None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # close-delimited bodies: streaming without chunked framing
    timeout = 120  # seconds a client may take to send its request

    def _refuse(self, code: int, reason: str) -> None:
        body = json.dumps({"error": {"message": reason, "type": "atolye_front_filter"}}).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _client_gone(self) -> bool:
        try:
            return self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
        except BlockingIOError:
            return False
        except OSError:
            return True

    def _wait_for_upstream(self, conn: http.client.HTTPConnection) -> bool:
        """Wait until the upstream answers; False if the client went away first (then the upstream is dropped,
        so the model server can abort the generation instead of finishing it for nobody)."""
        while True:
            ready, _, _ = select.select([conn.sock, self.connection], [], [], 5)
            if conn.sock in ready:
                return True
            if self.connection in ready and self._client_gone():
                return False

    def _proxy(self, method: str) -> None:
        path = checked_path(self.path)
        if path is None:
            return self._refuse(404, "path not allowed")
        if self.headers.get("transfer-encoding"):
            return self._refuse(411, "send a content-length, not a transfer-encoding")
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            return self._refuse(400, "bad content-length")
        if length < 0:
            return self._refuse(400, "bad content-length")
        if length > MAX_BODY:
            return self._refuse(413, "request too large")
        body = self.rfile.read(length) if length else None
        if body is not None and len(body) != length:
            return self._refuse(400, "request body shorter than its content-length")
        headers = {h: v for h in REQUEST_HEADERS if (v := self.headers.get(h)) is not None}
        headers["accept-encoding"] = "identity"  # plaintext responses, so redaction can see them
        if body is not None:
            headers["content-length"] = str(len(body))
        conn = http.client.HTTPConnection(UPSTREAM.hostname, UPSTREAM.port or 80, timeout=900)
        started = False
        try:
            conn.request(method, path, body=body, headers=headers)
            if not self._wait_for_upstream(conn):
                print(json.dumps({"event": "client_gone", "path": path}), file=sys.stderr, flush=True)
                return
            resp = conn.getresponse()
            self.send_response(resp.status)
            for h in RESPONSE_HEADERS:
                if (v := resp.getheader(h)) is not None:
                    self.send_header(h, v)
            self.end_headers()
            started = True
            red = Redactor(current_secret_forms())
            while chunk := resp.read1(65536):
                if out := red.feed(chunk):
                    self.wfile.write(out)  # raises if the client went away; the upstream is then dropped
                    self.wfile.flush()
            self.wfile.write(red.flush())
            if red.count:
                print(json.dumps({"event": "redacted", "path": path, "count": red.count}), file=sys.stderr, flush=True)
        except (OSError, http.client.HTTPException) as exc:
            if not started:  # after the status line, the only honest signal left is closing the stream
                try:
                    self._refuse(502, f"upstream error: {type(exc).__name__}")
                except OSError:
                    pass  # the client is gone too
        finally:
            conn.close()

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        self._proxy("POST")

    def do_GET(self) -> None:  # noqa: N802
        self._proxy("GET")

    def log_message(self, fmt: str, *args) -> None:  # one line per request, no headers, no bodies
        print(json.dumps({"event": "request", "line": fmt % args}), file=sys.stderr, flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
