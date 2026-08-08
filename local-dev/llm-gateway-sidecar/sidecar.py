#!/usr/bin/env python3
"""OpenAI-compatible LLM auth sidecar for NBI local / Hub spike.

Implements stories LLM-S01–S04, S13–S16 (local):
  - mock / jar / static token mint + refresh
  - proxy or mock chat completions
  - quota check/commit via local file store or HTTP Quota Service
  - /healthz, /quota, /metrics
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

# Allow `python sidecar.py` from this directory
_SIDECAR_DIR = Path(__file__).resolve().parent
if str(_SIDECAR_DIR) not in sys.path:
    sys.path.insert(0, str(_SIDECAR_DIR))

from quota_store import build_quota_backend  # noqa: E402
from token_provider import build_token_provider  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="[sidecar] %(levelname)s %(message)s",
)
log = logging.getLogger("sidecar")

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8089"))
MODE = os.environ.get("MODE", "mock").strip().lower()
MODEL_ID = os.environ.get("MODEL_ID", "databricks/gdp-gpt4o")
USER_ID = os.environ.get("NBI_LLM_USER", os.environ.get("JUPYTERHUB_USER", "local-dev"))
GROUPS = [g for g in os.environ.get("NBI_LLM_GROUPS", "").split(",") if g]
# Hint only — enforcement uses Quota Service / store resolver (LLM-S15)
HINT_PLAN = os.environ.get("NBI_LLM_PLAN", "local")
UPSTREAM_BASE_URL = os.environ.get("UPSTREAM_BASE_URL", "").rstrip("/")
UPSTREAM_VERIFY_TLS = os.environ.get("UPSTREAM_VERIFY_TLS", "1") not in ("0", "false", "False")
CA_BUNDLE = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or ""

_started_at = time.time()
_metrics = {
    "requests_total": 0,
    "tokens_total": 0,
    "denials_total": 0,
    "upstream_errors_total": 0,
    "mint_failures_total": 0,
    "soft_cap_hits_total": 0,
    # feature → count (chat|inline|agent)
    "requests_by_feature": {},
    "tokens_by_feature": {},
}
_metrics_lock = threading.Lock()

token_provider = build_token_provider()
quota = build_quota_backend()


def _inc(metric: str, n: int = 1) -> None:
    with _metrics_lock:
        _metrics[metric] = int(_metrics.get(metric, 0)) + n


def _inc_feature(bucket: str, feature: str, n: int = 1) -> None:
    feat = (feature or "chat").strip().lower() or "chat"
    if feat not in ("chat", "inline", "agent"):
        feat = "chat"
    with _metrics_lock:
        m = _metrics.setdefault(bucket, {})
        m[feat] = int(m.get(feat, 0)) + n


def redact_secrets(text: str) -> str:
    """Strip Bearer tokens / Authorization headers from log lines (LLM-S16.4)."""
    import re

    out = re.sub(r"(?i)(authorization\s*[:=]\s*)bearer\s+\S+", r"\1Bearer [REDACTED]", text)
    out = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-+/=]+", "Bearer [REDACTED]", out)
    return out


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
    snap = quota.snapshot(USER_ID, groups=GROUPS) if hasattr(quota, "snapshot") else {}
    plan = snap.get("plan_id", HINT_PLAN)
    return (
        f"[local-dev mock · plan={plan} · user={USER_ID}]\n"
        f"Echo: {snippet}\n\n"
        "```python\nprint('hello from local LLM sidecar')\n```\n"
    )


def build_completion_payload(model: str, content: str, pt: int, ct: int) -> dict[str, Any]:
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
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
        },
    }


def stream_chunks(model: str, content: str, pt: int, ct: int):
    cid = f"chatcmpl-local-{int(time.time() * 1000)}"
    yield {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    step = max(8, len(content) // 6) or 8
    for i in range(0, len(content), step):
        yield {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {"index": 0, "delta": {"content": content[i : i + step]}, "finish_reason": None}
            ],
        }
    yield {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
    }


def _ssl_context() -> Optional[ssl.SSLContext]:
    if not UPSTREAM_VERIFY_TLS:
        log.warning("UPSTREAM_VERIFY_TLS=0 — TLS verification disabled (sidecar only)")
        return ssl._create_unverified_context()
    if CA_BUNDLE:
        ctx = ssl.create_default_context(cafile=CA_BUNDLE)
        return ctx
    return ssl.create_default_context()


def get_bearer(force_refresh: bool = False) -> str:
    try:
        return token_provider.get_token(force_refresh=force_refresh).access_token
    except Exception:
        _inc("mint_failures_total")
        raise


def proxy_upstream(body: bytes) -> tuple[int, bytes]:
    if not UPSTREAM_BASE_URL:
        raise RuntimeError("UPSTREAM_BASE_URL is required in proxy mode")
    url = f"{UPSTREAM_BASE_URL}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {get_bearer()}",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    ctx = _ssl_context() if urlparse(url).scheme == "https" else None
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # One forced refresh then retry (LLM-S03.2)
            log.warning("upstream 401 — forcing token refresh")
            headers["Authorization"] = f"Bearer {get_bearer(force_refresh=True)}"
            req2 = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req2, context=ctx, timeout=120) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as e2:
                return e2.code, e2.read()
        return e.code, e.read()


def quota_check(estimate: int, model: str, feature: str = "chat") -> Any:
    return quota.check(
        username=USER_ID,
        estimate_tokens=estimate,
        model=model,
        groups=GROUPS,
        hint_plan=HINT_PLAN,
        feature=feature,
    )


def quota_commit(tokens: int, model: str, feature: str) -> None:
    if hasattr(quota, "commit"):
        quota.commit(
            username=USER_ID,
            tokens=tokens,
            model=model,
            feature=feature,
            groups=GROUPS,
        )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        msg = redact_secrets(fmt % args)
        sys.stderr.write(f"[sidecar] {self.address_string()} {msg}\n")

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
        return json.loads(raw.decode("utf-8") or "{}")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/healthz", "/health"):
            warm = True
            try:
                get_bearer()
            except Exception as exc:  # noqa: BLE001
                warm = False
                self._send(
                    503,
                    {"status": "degraded", "token_warm": False, "error": str(exc)[:200]},
                )
                return
            self._send(
                200,
                {
                    "status": "ok",
                    "mode": MODE,
                    "token_provider": os.environ.get("TOKEN_PROVIDER", "mock"),
                    "token_warm": warm,
                    "uptime_s": int(time.time() - _started_at),
                    "user_id": USER_ID,
                    "groups": GROUPS,
                },
            )
            return
        if path in ("/quota", "/v1/quota"):
            if hasattr(quota, "snapshot"):
                snap = quota.snapshot(USER_ID, groups=GROUPS)
            else:
                snap = quota.snapshot(USER_ID, groups=GROUPS)
            snap["mode"] = MODE
            snap["model_id"] = MODEL_ID
            self._send(200, snap)
            return
        if path in ("/metrics",):
            with _metrics_lock:
                lines = [
                    "# HELP nbi_llm_requests_total LLM completion requests",
                    "# TYPE nbi_llm_requests_total counter",
                    f'nbi_llm_requests_total{{user="{USER_ID}"}} {_metrics["requests_total"]}',
                    "# HELP nbi_llm_tokens_total LLM tokens committed",
                    "# TYPE nbi_llm_tokens_total counter",
                    f'nbi_llm_tokens_total{{user="{USER_ID}"}} {_metrics["tokens_total"]}',
                    "# HELP nbi_llm_denials_total Quota denials",
                    "# TYPE nbi_llm_denials_total counter",
                    f'nbi_llm_denials_total{{user="{USER_ID}"}} {_metrics["denials_total"]}',
                    "# HELP nbi_llm_soft_cap_hits_total Soft-cap (≥80%) observations (alert only)",
                    "# TYPE nbi_llm_soft_cap_hits_total counter",
                    f'nbi_llm_soft_cap_hits_total{{user="{USER_ID}"}} {_metrics["soft_cap_hits_total"]}',
                    "# HELP nbi_llm_upstream_errors_total Upstream errors",
                    "# TYPE nbi_llm_upstream_errors_total counter",
                    f"nbi_llm_upstream_errors_total {_metrics['upstream_errors_total']}",
                    "# HELP nbi_llm_mint_failures_total Token mint failures",
                    "# TYPE nbi_llm_mint_failures_total counter",
                    f"nbi_llm_mint_failures_total {_metrics['mint_failures_total']}",
                    "# HELP nbi_llm_requests_by_feature_total Requests by X-NBI-Feature",
                    "# TYPE nbi_llm_requests_by_feature_total counter",
                ]
                for feat, n in sorted((_metrics.get("requests_by_feature") or {}).items()):
                    lines.append(
                        f'nbi_llm_requests_by_feature_total{{user="{USER_ID}",feature="{feat}"}} {n}'
                    )
                lines += [
                    "# HELP nbi_llm_tokens_by_feature_total Tokens by X-NBI-Feature",
                    "# TYPE nbi_llm_tokens_by_feature_total counter",
                ]
                for feat, n in sorted((_metrics.get("tokens_by_feature") or {}).items()):
                    lines.append(
                        f'nbi_llm_tokens_by_feature_total{{user="{USER_ID}",feature="{feat}"}} {n}'
                    )
            body = ("\n".join(lines) + "\n").encode("utf-8")
            self._send(200, body, content_type="text/plain; version=0.0.4")
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

        t0 = time.time()
        try:
            req = self._read_json()
        except json.JSONDecodeError:
            self._send(400, {"error": {"message": "invalid JSON", "type": "invalid_request"}})
            return

        messages = req.get("messages") or []
        model = req.get("model") or MODEL_ID
        stream = bool(req.get("stream"))
        feature = self.headers.get("X-NBI-Feature", "chat")
        estimate = estimate_tokens_from_messages(messages)

        try:
            decision = quota_check(estimate, model, feature=feature)
        except Exception as exc:  # noqa: BLE001
            # Fail closed when Quota Service / store is unreachable (LLM-S18.2)
            log.error("quota check failed (fail-closed): %s", exc)
            self._send(
                503,
                {
                    "error": {
                        "message": "quota backend unavailable; refusing LLM request",
                        "type": "quota_unavailable",
                        "code": "quota_unavailable",
                    }
                },
            )
            return
        if not decision.allowed:
            _inc("denials_total")
            self._send(
                429,
                {
                    "error": {
                        "message": decision.message or "quota exceeded",
                        "type": "quota_exceeded",
                        "code": "quota_exceeded",
                        "plan": decision.plan_id,
                        "reset_at": decision.reset_at,
                    }
                },
            )
            return

        _inc("requests_total")
        _inc_feature("requests_by_feature", feature)
        if decision.soft_cap_hit:
            _inc("soft_cap_hits_total")
            log.warning(
                "soft cap (≥80%%) for user=%s plan=%s feature=%s (alert only; request allowed)",
                USER_ID,
                decision.plan_id,
                feature,
            )

        if MODE == "proxy":
            try:
                status, raw = proxy_upstream(json.dumps(req).encode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                _inc("upstream_errors_total")
                self._send(
                    502,
                    {"error": {"message": f"upstream error: {exc}", "type": "upstream_error"}},
                )
                return
            used = estimate
            if not stream:
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                    usage = parsed.get("usage") or {}
                    used = int(usage.get("total_tokens") or estimate)
                except Exception:  # noqa: BLE001
                    pass
            quota_commit(used, model, feature)
            _inc("tokens_total", used)
            _inc_feature("tokens_by_feature", feature, used)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("X-NBI-Latency-Ms", str(int((time.time() - t0) * 1000)))
            self.send_header("X-NBI-Feature", feature)
            if decision.soft_cap_hit:
                self.send_header("X-NBI-Quota-Soft-Cap", "1")
            self.end_headers()
            self.wfile.write(raw)
            return

        content = mock_reply_text(messages)
        pt, ct = estimate, max(1, len(content) // 4)
        used = pt + ct
        quota_commit(used, model, feature)
        _inc("tokens_total", used)
        _inc_feature("tokens_by_feature", feature, used)

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in stream_chunks(model, content, pt, ct):
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        payload = build_completion_payload(model, content, pt, ct)
        # Soft-cap is informational; include flag in JSON for local UX/tests.
        if decision.soft_cap_hit:
            payload["nbi_quota_soft_cap"] = True
        self._send(200, payload)


def main() -> int:
    if MODE not in ("mock", "proxy"):
        log.error("MODE must be mock|proxy, got %r", MODE)
        return 2
    if MODE == "proxy" and not UPSTREAM_BASE_URL:
        log.error("UPSTREAM_BASE_URL required for MODE=proxy")
        return 2
    if HOST not in ("127.0.0.1", "localhost", "::1") and os.environ.get(
        "SIDECAR_ALLOW_NON_LOOPBACK", ""
    ) != "1":
        log.error(
            "Refusing to bind %s (loopback only). Set SIDECAR_ALLOW_NON_LOOPBACK=1 to override.",
            HOST,
        )
        return 2

    # Warm token at start (LLM-S02.2)
    try:
        get_bearer()
        log.info("token warm OK")
    except Exception as exc:  # noqa: BLE001
        log.warning("token warm failed at startup: %s", exc)

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info(
        "listening on http://%s:%s mode=%s model=%s user=%s token_provider=%s quota=%s",
        HOST,
        PORT,
        MODE,
        MODEL_ID,
        USER_ID,
        os.environ.get("TOKEN_PROVIDER", "mock"),
        os.environ.get("QUOTA_BACKEND", "local"),
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
