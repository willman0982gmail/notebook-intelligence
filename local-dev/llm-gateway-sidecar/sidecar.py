#!/usr/bin/env python3
"""Minimal OpenAI-compatible LLM auth sidecar for local NBI testing.

Modes
-----
mock  (default)  Return deterministic chat completions (no upstream).
proxy            Forward to UPSTREAM_BASE_URL with Authorization: Bearer TOKEN.

Env
---
HOST                 bind address (default: 127.0.0.1)
PORT                 listen port (default: 8089)
MODE                 mock | proxy (default: mock)
MODEL_ID             default model id (default: databricks/gdp-gpt4o)
NBI_LLM_USER         subject for quota (default: local-dev)
NBI_LLM_PLAN         plan label (default: local)
QUOTA_TOKENS_DAY     hard daily token budget; 0 = unlimited (default: 50000)
UPSTREAM_BASE_URL    e.g. https://gateway.example.com/v1  (proxy mode)
UPSTREAM_API_KEY     bearer for upstream (proxy mode)
UPSTREAM_VERIFY_TLS  1/0 (default: 1). Set 0 only for local corp spikes.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlparse

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8089"))
MODE = os.environ.get("MODE", "mock").strip().lower()
MODEL_ID = os.environ.get("MODEL_ID", "databricks/gdp-gpt4o")
USER_ID = os.environ.get("NBI_LLM_USER", "local-dev")
PLAN_ID = os.environ.get("NBI_LLM_PLAN", "local")
QUOTA_TOKENS_DAY = int(os.environ.get("QUOTA_TOKENS_DAY", "50000"))
UPSTREAM_BASE_URL = os.environ.get("UPSTREAM_BASE_URL", "").rstrip("/")
UPSTREAM_API_KEY = os.environ.get("UPSTREAM_API_KEY", "")
UPSTREAM_VERIFY_TLS = os.environ.get("UPSTREAM_VERIFY_TLS", "1") not in (
    "0",
    "false",
    "False",
)

_lock = threading.Lock()
_used_tokens = 0
_window_start = time.time()
_started_at = time.time()


def _reset_window_if_needed() -> None:
    global _used_tokens, _window_start
    # Simple rolling 24h window for local tests.
    if time.time() - _window_start >= 86400:
        _used_tokens = 0
        _window_start = time.time()


def quota_snapshot() -> dict[str, Any]:
    with _lock:
        _reset_window_if_needed()
        limit = QUOTA_TOKENS_DAY
        return {
            "user_id": USER_ID,
            "plan_id": PLAN_ID,
            "used_tokens": _used_tokens,
            "limit_tokens": limit if limit > 0 else None,
            "remaining_tokens": None if limit <= 0 else max(0, limit - _used_tokens),
            "reset_at": int(_window_start + 86400),
            "mode": MODE,
            "model_id": MODEL_ID,
        }


def check_and_reserve(estimate: int) -> Optional[str]:
    """Return error message if denied, else None."""
    global _used_tokens
    if QUOTA_TOKENS_DAY <= 0:
        return None
    with _lock:
        _reset_window_if_needed()
        if _used_tokens + max(estimate, 0) > QUOTA_TOKENS_DAY:
            return (
                f"LLM daily quota exceeded (plan={PLAN_ID}, "
                f"used={_used_tokens}, limit={QUOTA_TOKENS_DAY}). "
                f"Resets at unix={int(_window_start + 86400)}."
            )
        return None


def commit_tokens(n: int) -> None:
    global _used_tokens
    if n <= 0:
        return
    with _lock:
        _reset_window_if_needed()
        _used_tokens += n


def estimate_tokens_from_messages(messages: list) -> int:
    total = 0
    for m in messages or []:
        content = m.get("content") if isinstance(m, dict) else ""
        if isinstance(content, str):
            total += max(1, len(content) // 4)
        elif isinstance(content, list):
            total += 32
    return total + 16


def mock_reply_text(messages: list) -> str:
    last_user = ""
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content", "")
            last_user = c if isinstance(c, str) else str(c)
            break
    snippet = (last_user or "(empty)").strip().replace("\n", " ")
    if len(snippet) > 120:
        snippet = snippet[:117] + "..."
    return (
        f"[local-dev mock · plan={PLAN_ID} · user={USER_ID}]\n"
        f"Echo: {snippet}\n\n"
        "```python\n"
        "print('hello from local LLM sidecar')\n"
        "```\n"
    )


def build_completion_payload(
    model: str, content: str, prompt_tokens: int, completion_tokens: int
) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-local-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def stream_chunks(model: str, content: str, prompt_tokens: int, completion_tokens: int):
    cid = f"chatcmpl-local-{int(time.time() * 1000)}"
    # role chunk
    yield {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    # content in small pieces for NBI streaming UX
    step = max(8, len(content) // 6) or 8
    for i in range(0, len(content), step):
        piece = content[i : i + step]
        yield {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
        }
    yield {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def proxy_upstream(body: bytes, stream: bool) -> tuple[int, dict, bytes]:
    if not UPSTREAM_BASE_URL:
        raise RuntimeError("UPSTREAM_BASE_URL is required in proxy mode")
    url = f"{UPSTREAM_BASE_URL}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {UPSTREAM_API_KEY or 'unused'}",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    ctx = None
    if urlparse(url).scheme == "https" and not UPSTREAM_VERIFY_TLS:
        ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
            raw = resp.read()
            return resp.status, dict(resp.headers.items()), raw
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()), e.read()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[sidecar] {self.address_string()} {fmt % args}\n")

    def _send(self, code: int, payload: Any, content_type: str = "application/json") -> None:
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload).encode("utf-8")
        elif isinstance(payload, bytes):
            data = payload
        else:
            data = str(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/healthz", "/health"):
            self._send(
                200,
                {
                    "status": "ok",
                    "mode": MODE,
                    "uptime_s": int(time.time() - _started_at),
                    "user_id": USER_ID,
                },
            )
            return
        if path in ("/quota", "/v1/quota"):
            self._send(200, quota_snapshot())
            return
        if path in ("/v1/models", "/models"):
            self._send(
                200,
                {
                    "object": "list",
                    "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local-dev"}],
                },
            )
            return
        self._send(404, {"error": {"message": f"not found: {path}", "type": "not_found"}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._send(404, {"error": {"message": f"not found: {path}", "type": "not_found"}})
            return

        try:
            req = self._read_json()
        except json.JSONDecodeError:
            self._send(400, {"error": {"message": "invalid JSON", "type": "invalid_request"}})
            return

        messages = req.get("messages") or []
        model = req.get("model") or MODEL_ID
        stream = bool(req.get("stream"))
        estimate = estimate_tokens_from_messages(messages)

        denied = check_and_reserve(estimate)
        if denied:
            self._send(
                429,
                {
                    "error": {
                        "message": denied,
                        "type": "quota_exceeded",
                        "code": "quota_exceeded",
                    }
                },
            )
            return

        if MODE == "proxy":
            body = json.dumps(req).encode("utf-8")
            try:
                status, _headers, raw = proxy_upstream(body, stream)
            except Exception as exc:  # noqa: BLE001
                self._send(
                    502,
                    {"error": {"message": f"upstream error: {exc}", "type": "upstream_error"}},
                )
                return
            # Best-effort usage commit for non-stream JSON
            used = estimate
            if not stream:
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                    usage = parsed.get("usage") or {}
                    used = int(usage.get("total_tokens") or estimate)
                except Exception:  # noqa: BLE001
                    pass
            commit_tokens(used)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        # mock mode
        content = mock_reply_text(messages)
        prompt_tokens = estimate
        completion_tokens = max(1, len(content) // 4)
        commit_tokens(prompt_tokens + completion_tokens)

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in stream_chunks(model, content, prompt_tokens, completion_tokens):
                line = f"data: {json.dumps(chunk)}\n\n".encode("utf-8")
                self.wfile.write(line)
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        self._send(
            200,
            build_completion_payload(model, content, prompt_tokens, completion_tokens),
        )


def main() -> int:
    if MODE not in ("mock", "proxy"):
        print(f"ERROR: MODE must be mock|proxy, got {MODE!r}", file=sys.stderr)
        return 2
    if MODE == "proxy" and not UPSTREAM_BASE_URL:
        print("ERROR: UPSTREAM_BASE_URL required for MODE=proxy", file=sys.stderr)
        return 2

    # Safety: only loopback by default
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(
        f"[sidecar] listening on http://{HOST}:{PORT}  mode={MODE}  "
        f"model={MODEL_ID}  user={USER_ID}  quota={QUOTA_TOKENS_DAY or 'unlimited'}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[sidecar] stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
