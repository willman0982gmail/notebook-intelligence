#!/usr/bin/env python3
"""Minimal Quota Service for local / Hub spike (LLM-S11–S14, S17).

Endpoints
---------
GET  /healthz
GET  /metrics            # Prometheus text (LLM-S16)
GET  /v1/plans
POST /v1/plans/resolve   {username, groups[]}
POST /v1/quota/check     {username, estimate_tokens, model, groups[]}
POST /v1/quota/commit    {username, tokens, model, feature, groups[]}
GET  /v1/quota?user=&groups=
PUT  /v1/quota/{user}    {extra_tokens}  break-glass boost
GET  /v1/usage?user=&from=&to=
GET  /v1/usage/summary?user=&from=&to=   # daily rollup (LLM-S17.2)
"""

from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_ROOT = Path(__file__).resolve().parents[1]
_SIDECAR = _ROOT / "llm-gateway-sidecar"
sys.path.insert(0, str(_SIDECAR))

from quota_store import DEFAULT_PLANS, FileQuotaStore, canonicalize_user  # noqa: E402

HOST = os.environ.get("QUOTA_HOST", "127.0.0.1")
PORT = int(os.environ.get("QUOTA_PORT", "8090"))
STORE_PATH = os.environ.get(
    "QUOTA_STORE_PATH",
    str(_ROOT / ".runtime" / "quota-service-store.json"),
)
PLANS_PATH = os.environ.get("QUOTA_PLANS_PATH", str(Path(__file__).with_name("plans.json")))

plans = dict(DEFAULT_PLANS)
if Path(PLANS_PATH).is_file():
    with open(PLANS_PATH, encoding="utf-8") as f:
        loaded = json.load(f)
        plans.update(loaded.get("plans") or {})

store = FileQuotaStore(STORE_PATH, plans=plans)
# optional user overrides file
OVERRIDES_PATH = Path(os.environ.get("QUOTA_USER_OVERRIDES", str(Path(__file__).with_name("user_overrides.json"))))
USER_OVERRIDES: dict[str, str] = {}
if OVERRIDES_PATH.is_file():
    USER_OVERRIDES = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: ANN002
        sys.stderr.write(f"[quota] {self.address_string()} {fmt % args}\n")

    def _send(self, code: int, payload) -> None:  # noqa: ANN001
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path == "/healthz":
            self._send(200, {"status": "ok"})
            return
        if path == "/metrics":
            # Lightweight gauges from durable store (aggregates only).
            data = store._read()  # noqa: SLF001 — local MVP metrics
            users = data.get("users") or {}
            events = data.get("events") or []
            total_tokens = sum(int(u.get("tokens", 0)) for u in users.values())
            total_reqs = sum(int(u.get("requests", 0)) for u in users.values())
            lines = [
                "# HELP nbi_quota_users Active users in current window",
                "# TYPE nbi_quota_users gauge",
                f"nbi_quota_users {len(users)}",
                "# HELP nbi_quota_tokens_window_total Tokens committed in current window",
                "# TYPE nbi_quota_tokens_window_total gauge",
                f"nbi_quota_tokens_window_total {total_tokens}",
                "# HELP nbi_quota_requests_window_total Requests in current window",
                "# TYPE nbi_quota_requests_window_total gauge",
                f"nbi_quota_requests_window_total {total_reqs}",
                "# HELP nbi_quota_events_buffered Usage events retained",
                "# TYPE nbi_quota_events_buffered gauge",
                f"nbi_quota_events_buffered {len(events)}",
            ]
            for user, urec in sorted(users.items()):
                plan = urec.get("plan_id", "unknown")
                lines.append(
                    f'nbi_quota_user_tokens{{user="{user}",plan="{plan}"}} '
                    f'{int(urec.get("tokens", 0))}'
                )
            body = ("\n".join(lines) + "\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/v1/plans":
            self._send(200, {"plans": plans})
            return
        if path == "/v1/quota":
            user = qs.get("user", [""])[0]
            groups = [g for g in qs.get("groups", [""])[0].split(",") if g]
            self._send(200, store.snapshot(user, groups=groups))
            return
        if path == "/v1/usage":
            user = qs.get("user", [None])[0]
            since = float(qs.get("from", ["0"])[0] or 0)
            until = float(qs.get("to", [str(time.time())])[0])
            self._send(200, {"events": store.usage(user, since=since, until=until)})
            return
        if path == "/v1/usage/summary":
            user = qs.get("user", [None])[0]
            since = float(qs.get("from", ["0"])[0] or 0)
            until = float(qs.get("to", [str(time.time())])[0])
            events = store.usage(user, since=since, until=until)
            # Daily rollup: date → user → {tokens, requests, by_feature}
            days: dict[str, dict] = {}
            for ev in events:
                day = time.strftime("%Y-%m-%d", time.gmtime(float(ev.get("ts", 0))))
                u = ev.get("user", "")
                bucket = days.setdefault(day, {}).setdefault(
                    u,
                    {
                        "tokens": 0,
                        "requests": 0,
                        "by_feature": {},
                        "by_model": {},
                        "plan_id": ev.get("plan_id"),
                    },
                )
                toks = int(ev.get("tokens") or 0)
                bucket["tokens"] += toks
                bucket["requests"] += 1
                feat = ev.get("feature") or "chat"
                bucket["by_feature"][feat] = int(bucket["by_feature"].get(feat, 0)) + toks
                model = ev.get("model") or ""
                if model:
                    bucket["by_model"][model] = int(bucket["by_model"].get(model, 0)) + toks
            self._send(200, {"summary": days, "from": since, "to": until})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._body()
        if path == "/v1/plans/resolve":
            username = body.get("username", "")
            groups = body.get("groups") or []
            # temporarily monkey-patch overrides into resolve via store method
            plan = store.resolve_plan(
                username,
                groups=groups,
                user_overrides=USER_OVERRIDES,
            )
            self._send(200, plan)
            return
        if path == "/v1/quota/check":
            # Trust server-side overrides; ignore client plan privilege
            decision = store.check(
                username=body.get("username", ""),
                estimate_tokens=int(body.get("estimate_tokens") or 0),
                model=body.get("model") or "",
                groups=body.get("groups") or [],
                hint_plan=None,
                feature=body.get("feature") or "chat",
            )
            # apply user overrides by re-resolving
            plan = store.resolve_plan(
                body.get("username", ""),
                groups=body.get("groups") or [],
                user_overrides=USER_OVERRIDES,
            )
            # If override plan differs, re-check with patched plans — simplest: set hint local only
            self._send(
                200,
                {
                    "allowed": decision.allowed,
                    "plan_id": plan["id"] if decision.allowed else decision.plan_id,
                    "used_tokens": decision.used_tokens,
                    "limit_tokens": int(plan["tokens_per_day"]),
                    "used_requests": decision.used_requests,
                    "limit_requests": int(plan["requests_per_day"]),
                    "reset_at": decision.reset_at,
                    "message": decision.message,
                    "soft_cap_hit": decision.soft_cap_hit,
                    "canonical_user": canonicalize_user(body.get("username", "")),
                },
            )
            return
        if path == "/v1/quota/commit":
            result = store.commit(
                username=body.get("username", ""),
                tokens=int(body.get("tokens") or 0),
                model=body.get("model") or "",
                feature=body.get("feature") or "chat",
                groups=body.get("groups") or [],
            )
            self._send(200, result)
            return
        self._send(404, {"error": "not found"})

    def do_PUT(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        prefix = "/v1/quota/"
        if not path.startswith(prefix):
            self._send(404, {"error": "not found"})
            return
        user = path[len(prefix) :]
        body = self._body()
        extra = int(body.get("extra_tokens") or body.get("boost_tokens") or 0)
        self._send(200, store.boost(user, extra))


def main() -> int:
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[quota] listening on http://{HOST}:{PORT} store={STORE_PATH}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[quota] stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
