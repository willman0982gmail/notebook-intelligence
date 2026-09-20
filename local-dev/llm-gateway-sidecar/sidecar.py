#!/usr/bin/env python3
"""OpenAI-compatible LLM auth sidecar for NBI local / Hub spike.

This module is the **co-process** that runs alongside every singleuser
JupyterLab instance in a Hub pod (or on a developer laptop).  It exposes a
minimal OpenAI-compatible HTTP API on ``127.0.0.1:8089`` that NBI's
``openai-compatible`` provider talks to, and transparently handles:

* OIDC bearer token minting (via mock / jar / static provider) + refresh with
  skew protection, exponential-backoff retry, and 401-triggered forced refresh.
* Pre-request quota ``check`` and post-request ``commit`` against either a
  local file-backed store or the centralized Quota Service.
* Secret-safe logging (Authorization headers are redacted from error text).
* Operational endpoints: ``/healthz``, ``/quota``, ``/metrics``, ``/v1/models``.
* In ``MODE=mock`` it returns synthetic replies (no network access required)
  with streaming SSE support.
* In ``MODE=proxy`` it forwards the body verbatim to
  ``UPSTREAM_BASE_URL/chat/completions`` with a freshly minted Bearer token
  attached and returns the raw upstream payload to NBI.

Implements stories LLM-S01–S04 (token), S13–S16 (quota + ops endpoints),
S18 (fail-closed semantics), and S22 (per-feature token caps).
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Module bootstrap: allow `python sidecar.py` from inside this directory
# without first installing the package.  quota_store + token_provider live in
# the same folder; inserting _SIDECAR_DIR keeps "python …/sidecar.py" and
# "python sidecar.py" behave the same way.
# ---------------------------------------------------------------------------
_SIDECAR_DIR = Path(__file__).resolve().parent
if str(_SIDECAR_DIR) not in sys.path:
    sys.path.insert(0, str(_SIDECAR_DIR))

from quota_store import build_quota_backend  # noqa: E402
from token_provider import build_token_provider  # noqa: E402

# ---------------------------------------------------------------------------
# Global logging setup.  Format is intentionally terse — sidecar logs are
# captured to sidecar.log by entrypoint.sh and are typically viewed via
# `kubectl logs`; stack trace verbosity is already supplied by the logging
# module's formatter.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="[sidecar] %(levelname)s %(message)s",
)
log = logging.getLogger("sidecar")

# ---------------------------------------------------------------------------
# Startup-time configuration (read once; process-lifetime).
# All values come from the pod environment injected by the Hub
# pre_spawn_hook; defaults are provided for the laptop / docker-run case.
# ---------------------------------------------------------------------------
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8089"))
# MODE — "mock" (default, returns fake replies)  |  "proxy" (forwards upstream)
MODE = os.environ.get("MODE", "mock").strip().lower()
# Default model id advertised via /v1/models and used when NBI omits it.
MODEL_ID = os.environ.get("MODEL_ID", "databricks/gdp-gpt4o")
# Identity — preferred source is NBI_LLM_USER injected by the Hub
# pre_spawn_hook; fall back to JUPYTERHUB_USER for zero-config in a plain
# JH container, and finally "local-dev" for laptops.
USER_ID = os.environ.get("NBI_LLM_USER", os.environ.get("JUPYTERHUB_USER", "local-dev"))
# Group list — comma-separated; used as input to plan resolver (LLM-S15).
GROUPS = [g for g in os.environ.get("NBI_LLM_GROUPS", "").split(",") if g]
# Hint plan — UX ONLY.  Enforcement re-resolves server-side so this value is
# forgeable-safe (see quota_store.resolve_plan).
HINT_PLAN = os.environ.get("NBI_LLM_PLAN", "local")
# Upstream gateway — required for proxy mode.  Trailing slashes are stripped so
# callers can just append "/chat/completions".
UPSTREAM_BASE_URL = os.environ.get("UPSTREAM_BASE_URL", "").rstrip("/")
# TLS verify — allow disabling in dig environments (must be "1" in prod).
UPSTREAM_VERIFY_TLS = os.environ.get("UPSTREAM_VERIFY_TLS", "1") not in ("0", "false", "False")
# Optional custom CA bundle — honoured by both the Python ssl context and
# (through env propagation) by callers that use `requests`/`httpx`.
CA_BUNDLE = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or ""

# ---------------------------------------------------------------------------
# Issue #8: Precompiled regex patterns for redact_secrets (once at import).
# ---------------------------------------------------------------------------
_RE_AUTH_HEADER = re.compile(r"(?i)(authorization\s*[:=]\s*)bearer\s+\S+")
_RE_BARE_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-+/=]+")

# ---------------------------------------------------------------------------
# Issue #13: Max request body sizes.  Chat-completions can carry multi-modal
# payloads so it gets a larger ceiling; all other routes use 1 MiB.
# ---------------------------------------------------------------------------
MAX_BODY_DEFAULT = int(os.environ.get("SIDECAR_MAX_BODY_BYTES", str(1 * 1024 * 1024)))
MAX_BODY_CHAT = int(os.environ.get("SIDECAR_MAX_CHAT_BODY_BYTES", str(16 * 1024 * 1024)))

# ---------------------------------------------------------------------------
# Issue #10: ThreadingHTTPServer runtime limits.
# ---------------------------------------------------------------------------
_MAX_THREADS = int(os.environ.get("SIDECAR_MAX_THREADS", "32"))
_REQUEST_QUEUE_SIZE = int(os.environ.get("SIDECAR_REQUEST_QUEUE_SIZE", "64"))

# ---------------------------------------------------------------------------
# Issue #7: Allowlisted client headers forwarded upstream (opt-in list so
# random garbage headers from NBI never leak).
# ---------------------------------------------------------------------------
_FORWARDED_HEADERS = (
    "X-Request-ID",
    "X-Correlation-ID",
    "X-Trace-Id",
    "OpenAI-Organization",
    "OpenAI-Project",
)

# ---------------------------------------------------------------------------
# Prometheus-style counters.  Kept as a plain dict guarded by _metrics_lock.
# In production the /metrics endpoint must be scraped via kubectl exec (it
# only listens on loopback); in dig/staging `curl 127.0.0.1:8089/metrics` is
# enough.
# ---------------------------------------------------------------------------
_started_at = time.time()
_metrics = {
    "requests_total": 0,          # chat-completion requests (allowed only)
    "tokens_total": 0,            # tokens committed (post-request)
    "denials_total": 0,           # quota-based 429s
    "upstream_errors_total": 0,   # transport-level errors talking to AI Factory
    "mint_failures_total": 0,     # token mint failures after all retries
    "soft_cap_hits_total": 0,     # observations of used >= 80% limit
    # feature → count (chat|inline|agent)
    "requests_by_feature": {},
    "tokens_by_feature": {},
}
_metrics_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Module-level singletons.  Initialized eagerly (module import) so that any
# configuration error surfaces before we start the HTTP server.  The JAR
# provider's first mint is done lazily by get_bearer() in /healthz.
# ---------------------------------------------------------------------------
token_provider = build_token_provider()
quota = build_quota_backend()


def _inc(metric: str, n: int = 1) -> None:
    """Increment a scalar counter in the metrics dict (thread-safe)."""
    with _metrics_lock:
        _metrics[metric] = int(_metrics.get(metric, 0)) + n


def _inc_feature(bucket: str, feature: str, n: int = 1) -> None:
    """Increment a per-feature counter ("chat"/"inline"/"agent").

    Unknown feature names are silently collapsed to "chat" to keep the
    cardinality of exported timeseries low (prometheus best practice).
    """
    feat = (feature or "chat").strip().lower() or "chat"
    if feat not in ("chat", "inline", "agent"):
        feat = "chat"
    with _metrics_lock:
        m = _metrics.setdefault(bucket, {})
        m[feat] = int(m.get(feat, 0)) + n


def redact_secrets(text: str) -> str:
    """Strip Bearer tokens / Authorization headers from log lines (LLM-S16.4).

    Applied to every line emitted by the default BaseHTTPRequestHandler
    request logger, and should also be applied whenever an error message
    could contain an echo of the outbound Authorization header.

    Uses two module-level precompiled regexes:
      1. Full ``Authorization: Bearer <token>`` style (header form).
      2. Bare ``Bearer <token>`` substring form (stack traces / dict printouts).
    """
    out = _RE_AUTH_HEADER.sub(r"\1Bearer [REDACTED]", text)
    out = _RE_BARE_BEARER.sub("Bearer [REDACTED]", out)
    return out


def estimate_tokens_from_messages(messages: list) -> int:
    """Rough lower-bound estimator of prompt tokens (LLM-S13.2).

    Used for the pre-request quota ``check`` call when the caller does not
    know the eventual ``usage.total_tokens``.  The heuristic is intentionally
    conservative (len//4 chars per token) so we never under-count and
    accidentally pass a request that would be rejected after consuming
    upstream tokens.  Multi-modal content arrays are flat-counted as 32
    tokens per item to avoid requiring tiktoken as a dependency.
    """
    total = 0
    for m in messages or []:
        content = m.get("content") if isinstance(m, dict) else ""
        if isinstance(content, str):
            # 4 chars/token is a widely cited rough lower bound for English.
            total += max(1, len(content) // 4)
        elif isinstance(content, list):
            # Multi-modal array (image_url / text chunks): flat-penalty per part.
            total += 32
    # +16 for the assistant role template / chat template tokens.
    return total + 16


def mock_reply_text(messages: list) -> str:
    """Synthetic assistant reply for MODE=mock.

    Echoes the last user message + shows the resolved plan/user id so devs
    can visually confirm identity binding without issuing upstream calls.
    Includes a fenced Python code block so code-highlighting UX in NBI is
    exercisable in mock mode.
    """
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


_MOCK_SYSTEM_FINGERPRINT = f"nbi_sidecar_mock_{int(time.time()) // 86400}"


def build_completion_payload(model: str, content: str, pt: int, ct: int) -> dict[str, Any]:
    """Build a non-streaming ``chat.completion`` response dict in OpenAI format.

    Includes ``system_fingerprint`` and ``service_tier`` fields expected by
    modern OpenAI-compatible SDKs (Issue #16).
    """
    return {
        "id": f"chatcmpl-local-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "system_fingerprint": _MOCK_SYSTEM_FINGERPRINT,
        "service_tier": "mock",
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
    """Yield a sequence of SSE ``chat.completion.chunk`` dicts (mock mode).

    Output mirrors the real OpenAI streaming shape: first a chunk that
    announces the assistant role, then N chunks of delta content, then a
    final chunk with finish_reason="stop" plus the ``usage`` object so the
    caller can do accounting without having to reassemble the stream.

    ``system_fingerprint`` is attached to every chunk per the modern OpenAI
    SSE schema (Issue #16).
    """
    cid = f"chatcmpl-local-{int(time.time() * 1000)}"
    sf = _MOCK_SYSTEM_FINGERPRINT
    # Role-only opening chunk (mirrors OpenAI behaviour).
    yield {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "system_fingerprint": sf,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    # Content chunks — split into at least 6 pieces so progress UX is visible
    # even for short replies.
    step = max(8, len(content) // 6) or 8
    for i in range(0, len(content), step):
        yield {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "system_fingerprint": sf,
            "choices": [
                {"index": 0, "delta": {"content": content[i : i + step]}, "finish_reason": None}
            ],
        }
    # Terminal chunk: stop finish reason + aggregated usage.
    yield {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "system_fingerprint": sf,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
    }


def _ssl_context() -> Optional[ssl.SSLContext]:
    """Build the SSL context for upstream HTTPS calls.

    Priority:
      1. ``UPSTREAM_VERIFY_TLS=0`` → unverified context (dev/dig only, with a
         WARNING log).
      2. ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE`` env present → default
         context with custom cafile (corp CA PEM bundle).
      3. Default system CA context.
    """
    if not UPSTREAM_VERIFY_TLS:
        log.warning("UPSTREAM_VERIFY_TLS=0 — TLS verification disabled (sidecar only)")
        return ssl._create_unverified_context()
    if CA_BUNDLE:
        ctx = ssl.create_default_context(cafile=CA_BUNDLE)
        return ctx
    return ssl.create_default_context()


def get_bearer(force_refresh: bool = False) -> str:
    """Return a fresh Bearer access_token string.

    Wraps the configured token_provider with a simple failure counter so
    /metrics can observe IdP flakiness.  ``force_refresh=True`` is used by
    the 401-retry branch in proxy_upstream() after an upstream 401 suggests
    the cached token was revoked or has a clock-skew issue.

    Raises the underlying exception from TokenProvider on failure (after
    RetryingTokenProvider has exhausted its retries).
    """
    try:
        return token_provider.get_token(force_refresh=force_refresh).access_token
    except Exception:
        _inc("mint_failures_total")
        raise


def _build_upstream_headers(client_headers=None) -> dict[str, str]:
    """Construct headers for an upstream AI Factory call (Issues #2, #7).

    - Content-Type: application/json (fixed; always set)
    - Authorization: Bearer $fresh_token (always set; caller handles refresh)
    - Allowlisted client headers (``_FORWARDED_HEADERS``) are copied from the
      incoming NBI request so trace IDs propagate end-to-end.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {get_bearer()}",
    }
    if client_headers is not None:
        for name in _FORWARDED_HEADERS:
            val = client_headers.get(name)
            if val:
                headers[name] = val
    return headers


def proxy_upstream(
    body: bytes,
    stream: bool = False,
    client_headers=None,
) -> tuple[int, str, bytes | None, Iterator[bytes] | None]:
    """Forward a chat-completions body to the AI Factory gateway (Issues #2, #7).

    Protocol notes (updated)
    ------------------------
    * On HTTP 401: attempt exactly ONE forced token refresh + retry.
      Retry re-uses the same allowlisted client headers as the first call so
      trace IDs stay consistent across both legs.
    * ``stream=True``: upstream ``text/event-stream`` responses are returned
      as a chunked Iterator (Issue #2).  All other responses (including
      non-stream JSON) are returned as a single bytes payload.
    * Returns ``(http_status, content_type, raw_bytes_if_nonstream, iter_if_stream)``.
      Exactly ONE of (raw_bytes, iter_if_stream) is non-None.
    """
    if not UPSTREAM_BASE_URL:
        raise RuntimeError("UPSTREAM_BASE_URL is required in proxy mode")
    url = f"{UPSTREAM_BASE_URL}/chat/completions"
    headers = _build_upstream_headers(client_headers)
    ctx = _ssl_context() if urlparse(url).scheme == "https" else None

    def _chunks(resp) -> Iterator[bytes]:
        # True chunked passthrough for SSE: 4 KiB per read, yield immediately
        # so NBI clients see incremental tokens without waiting for full
        # upstream download (Issue #2 fix).
        while True:
            buf = resp.read(4096)
            if not buf:
                break
            yield buf

    def _once(retry_auth: bool = False) -> tuple[int, str, bytes | None, Iterator[bytes] | None]:
        hdrs = dict(headers)
        if retry_auth:
            hdrs["Authorization"] = f"Bearer {get_bearer(force_refresh=True)}"
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
                ctype = resp.headers.get("Content-Type", "application/json") or "application/json"
                is_sse = stream and ("text/event-stream" in ctype.lower())
                if is_sse:
                    return resp.status, ctype, None, _chunks(resp)
                # Non-SSE / non-stream: one-shot read (original behaviour).
                return resp.status, ctype, resp.read(), None
        except urllib.error.HTTPError as e:
            raw = e.read()
            ctype = e.headers.get("Content-Type", "application/json") or "application/json"
            if e.code == 401 and not retry_auth:
                log.warning("upstream 401 — forcing token refresh and retrying")
                return _once(retry_auth=True)
            return e.code, ctype, raw, None

    return _once(retry_auth=False)


def quota_check(estimate: int, model: str, feature: str = "chat") -> Any:
    """Thin helper — closes over the module-level (USER_ID, GROUPS, HINT_PLAN)."""
    return quota.check(
        username=USER_ID,
        estimate_tokens=estimate,
        model=model,
        groups=GROUPS,
        hint_plan=HINT_PLAN,
        feature=feature,
    )


def quota_commit(tokens: int, model: str, feature: str) -> None:
    """Post-request commit; duck-typing-safe for both FileQuotaStore and HttpQuotaClient."""
    if hasattr(quota, "commit"):
        quota.commit(
            username=USER_ID,
            tokens=tokens,
            model=model,
            feature=feature,
            groups=GROUPS,
        )


class Handler(BaseHTTPRequestHandler):
    """Request handler for the sidecar's ThreadingHTTPServer.

    Routing is intentionally done via explicit ``if path in (...)`` chains
    rather than a regex/dispatch table — there are only 6 distinct routes in
    total and the flat structure is easier to audit during security reviews.

    Threading note: ThreadingHTTPServer means each request runs on its own
    thread.  All mutations of shared state (_metrics, quota store, token
    provider cache) go through explicit locks.
    """

    # Use HTTP/1.1 so we can emit Content-Length (and thus Connection: keep-
    # alive where useful).  BaseHTTPRequestHandler only speaks 1.0 by
    # default; we override protocol_version here.
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        """Override BaseHTTPRequestHandler logger to redact secrets (LLM-S16.4)."""
        msg = redact_secrets(fmt % args)
        sys.stderr.write(f"[sidecar] {self.address_string()} {msg}\n")

    def _send(self, code: int, payload: Any, content_type: str = "application/json") -> None:
        """Unified response helper — accepts dict/list/bytes/str."""
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload).encode("utf-8")
        elif isinstance(payload, bytes):
            data = payload
        else:
            data = str(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        # Explicit no-store on all responses — tokens/quota data must never
        # be cached by any intermediate proxy or browser cache.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self, max_bytes: int = MAX_BODY_DEFAULT) -> dict:
        """Read and JSON-parse the request body (empty body → {}).

        ``max_bytes`` caps Content-Length to prevent oversized-request DoS
        (Issue #13).  Returns HTTP 413 via RuntimeError caller if exceeded.
        """
        raw_len = int(self.headers.get("Content-Length", "0") or 0)
        if raw_len > max_bytes:
            self._send(
                413,
                {
                    "error": {
                        "message": f"payload too large: {raw_len} > max {max_bytes}",
                        "type": "payload_too_large",
                    }
                },
            )
            raise RuntimeError("body exceeds max bytes — response already sent")
        raw = self.rfile.read(raw_len) if raw_len else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    # ------------------------------------------------------------------
    # GET /healthz, /quota, /metrics, /v1/models
    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 — http.server convention
        path = self.path.split("?", 1)[0]

        # /healthz — readiness + token-warmth probe (LLM-S02.2).
        # Hub probes this from entrypoint.sh before launching JupyterLab so a
        # pod that can't mint tokens never enters Ready state.
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

        # /quota — current snapshot (powers NBI chat-sidebar badge).
        # ``hasattr`` + identical call is a redundant guard against the
        # future case where quota might be an object that lacks snapshot.
        if path in ("/quota", "/v1/quota"):
            if hasattr(quota, "snapshot"):
                snap = quota.snapshot(USER_ID, groups=GROUPS)
            else:
                snap = quota.snapshot(USER_ID, groups=GROUPS)
            snap["mode"] = MODE
            snap["model_id"] = MODEL_ID
            self._send(200, snap)
            return

        # /metrics — Prometheus exposition format.
        # User label is included on per-user timeseries to keep aggregation
        # simple; in central-prometheus deployments you'd instead use a
        # Pushgateway or kube-state-metrics style aggregation.
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

        # /v1/models — static single-entry list.  Intentionally minimal;
        # some OpenAI SDK clients call this on startup and a 404 triggers
        # loud warning logs on the client side.
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

    # ------------------------------------------------------------------
    # POST /v1/chat/completions — the one and only mutating route
    # ------------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802 — http.server convention
        path = self.path.split("?", 1)[0]
        if path not in ("/v1/chat/completions", "/chat/completions"):
            self._send(404, {"error": {"message": f"not found: {path}", "type": "not_found"}})
            return

        t0 = time.time()
        # --- 1. Parse input ------------------------------------------------
        try:
            req = self._read_json(max_bytes=MAX_BODY_CHAT)
        except RuntimeError:
            # _read_json already sent 413 to client; bail out.
            return
        except json.JSONDecodeError:
            self._send(400, {"error": {"message": "invalid JSON", "type": "invalid_request"}})
            return

        messages = req.get("messages") or []
        model = req.get("model") or MODEL_ID
        stream = bool(req.get("stream"))
        # X-NBI-Feature header distinguishes chat (default) vs. inline
        # completion vs. future agent traffic.
        feature = self.headers.get("X-NBI-Feature", "chat")
        estimate = estimate_tokens_from_messages(messages)

        # --- 2. Quota check (fail-closed) ----------------------------------
        try:
            decision = quota_check(estimate, model, feature=feature)
        except Exception as exc:  # noqa: BLE001
            # LLM-S18.2 — if quota backend is unreachable we deny the
            # request rather than let an unbounded number of upstream calls
            # slip through.  503 (not 429) signals this is an infra failure,
            # not a user-cap error.
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

        # --- 3. Book-keeping for allowed requests -------------------------
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

        # --- 4a. MODE=proxy: call upstream ---------------------------------
        if MODE == "proxy":
            try:
                status, ctype, raw, stream_iter = proxy_upstream(
                    json.dumps(req).encode("utf-8"),
                    stream=stream,
                    client_headers=self.headers,
                )
            except Exception as exc:  # noqa: BLE001
                # Connection errors, timeouts, SSL cert failures, etc.
                _inc("upstream_errors_total")
                self._send(
                    502,
                    {"error": {"message": f"upstream error: {exc}", "type": "upstream_error"}},
                )
                return

            # Committed token count: prefer real upstream usage.total_tokens
            # when available (non-stream case).  For SSE streaming the body
            # isn't fully parsed here — fall back to pre-request estimate.
            used = estimate
            if stream_iter is None and raw is not None and not stream:
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                    usage = parsed.get("usage") or {}
                    used = int(usage.get("total_tokens") or estimate)
                except Exception:  # noqa: BLE001
                    pass
            quota_commit(used, model, feature)
            _inc("tokens_total", used)
            _inc_feature("tokens_by_feature", feature, used)

            # --- Write response: chunked SSE or single-pass raw ------------
            if stream_iter is not None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("X-NBI-Latency-Ms", str(int((time.time() - t0) * 1000)))
                self.send_header("X-NBI-Feature", feature)
                if decision.soft_cap_hit:
                    self.send_header("X-NBI-Quota-Soft-Cap", "1")
                self.end_headers()
                for chunk in stream_iter:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                return

            # Non-stream: return upstream verbatim (original behaviour).
            data = raw or b""
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-NBI-Latency-Ms", str(int((time.time() - t0) * 1000)))
            self.send_header("X-NBI-Feature", feature)
            if decision.soft_cap_hit:
                self.send_header("X-NBI-Quota-Soft-Cap", "1")
            self.end_headers()
            self.wfile.write(data)
            return

        # --- 4b. MODE=mock: build a synthetic reply locally ---------------
        content = mock_reply_text(messages)
        pt, ct = estimate, max(1, len(content) // 4)
        used = pt + ct
        quota_commit(used, model, feature)
        _inc("tokens_total", used)
        _inc_feature("tokens_by_feature", feature, used)

        if stream:
            # SSE streaming — each chunk is prefixed with "data: " and a
            # blank line.  End of stream is marked by the literal string
            # "data: [DONE]" per OpenAI SSE convention.
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
    """Entry point: validate config → warm token → start HTTP server."""
    # --- Config guard rails (fail-fast at process start) ---------------
    if MODE not in ("mock", "proxy"):
        log.error("MODE must be mock|proxy, got %r", MODE)
        return 2
    if MODE == "proxy" and not UPSTREAM_BASE_URL:
        log.error("UPSTREAM_BASE_URL required for MODE=proxy")
        return 2
    # Bind-address guard: the sidecar handles un-scrubbed user content and
    # holds active bearer tokens in memory.  It MUST only bind loopback in
    # normal operation; SIDECAR_ALLOW_NON_LOOPBACK exists solely for the
    # docker-networked dig scenario where another container scrapes
    # /healthz.
    if HOST not in ("127.0.0.1", "localhost", "::1") and os.environ.get(
        "SIDECAR_ALLOW_NON_LOOPBACK", ""
    ) != "1":
        log.error(
            "Refusing to bind %s (loopback only). Set SIDECAR_ALLOW_NON_LOOPBACK=1 to override.",
            HOST,
        )
        return 2

    # --- Warm token at start (LLM-S02.2) --------------------------------
    # Non-fatal: if minting fails on the very first try we still start the
    # server (so the user's pod isn't blocked by an IdP blip), but /healthz
    # will report token_warm=false and readiness probes will fail.
    try:
        get_bearer()
        log.info("token warm OK")
    except Exception as exc:  # noqa: BLE001
        log.warning("token warm failed at startup: %s", exc)

    # --- Start server ---------------------------------------------------
    # Issue #10: BoundedThreadingHTTPServer caps worker threads via a
    # BoundedSemaphore so burst traffic cannot exhaust file descriptors or
    # memory.  request_queue_size sets the TCP listen(backlog) so clients
    # get a clean ECONNREFUSED rather than silent SYN drops beyond cap.
    #
    # The semaphore is acquired BEFORE spawning the handler thread and
    # released INSIDE the thread AFTER finish_request completes so the
    # count genuinely tracks in-flight handler threads (not just spawns).
    _thread_limiter = threading.BoundedSemaphore(_MAX_THREADS)

    class BoundedThreadingHTTPServer(ThreadingHTTPServer):
        """ThreadingHTTPServer with an upper bound on concurrent handlers."""

        daemon_threads = True

        def process_request(self, request, client_address) -> None:  # noqa: ANN001 — http.server signature
            if not _thread_limiter.acquire(timeout=1.0):
                try:
                    request.close()
                except Exception:  # noqa: BLE001
                    pass
                return

            def _run() -> None:
                try:
                    self.finish_request(request, client_address)
                except Exception:  # noqa: BLE001
                    self.handle_error(request, client_address)
                finally:
                    try:
                        self.shutdown_request(request)
                    finally:
                        _thread_limiter.release()

            t = threading.Thread(target=_run, name=f"sidecar-handler-{int(time.time() * 1000) % 100000}")
            t.daemon = True
            try:
                t.start()
            except RuntimeError:
                _thread_limiter.release()
                try:
                    request.close()
                except Exception:  # noqa: BLE001
                    pass

    httpd = BoundedThreadingHTTPServer((HOST, PORT), Handler)
    httpd.request_queue_size = _REQUEST_QUEUE_SIZE
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
