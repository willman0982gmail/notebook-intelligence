#!/usr/bin/env python3
"""Minimal Quota Service for local / Hub spike (LLM-S11–S14, S17).

This is the **centralized** counterpart to ``FileQuotaStore``.  When the
per-user sidecar uses ``QUOTA_BACKEND=http`` it calls this service so quota
counters survive pod churn, Notebook restarts, and are consistent across a
Hub cluster.

It is intentionally a thin HTTP wrapper around the shared ``FileQuotaStore``
class — all business logic (window rollover, plan resolution, user overrides,
event recording) lives in ``quota_store.py`` and is reused verbatim here.

Endpoints
---------
GET  /healthz             — always-ok readiness probe
GET  /metrics             — Prometheus text (LLM-S16)
GET  /v1/plans            — list known plan catalog
POST /v1/plans/resolve    {username, groups[]} → plan dict
POST /v1/quota/check      {username, estimate_tokens, model, groups[]} → decision
POST /v1/quota/commit     {username, tokens, model, feature, groups[]} → summary
GET  /v1/quota?user=&groups=  — snapshot for a single user
PUT  /v1/quota/{user}     {extra_tokens}  — break-glass admin boost
GET  /v1/usage?user=&from=&to=  — raw event list (optionally filtered)
GET  /v1/usage/summary?user=&from=&to=   — daily rollup (LLM-S17.2)

Authentication / Authorization
------------------------------
In this MVP there is **no** auth on Quota Service.  Network exposure is
controlled purely via K8s NetworkPolicy: only the Hub singleuser pods and
the Hub admin controller can reach port 8090.  Production deployments MUST
either: (a) add mTLS between sidecar → quota-service, or (b) add a service
account JWT check, or (c) only bind on a cluster-internal IP + run behind a
service mesh with RBAC on calls to ``/v1/quota/{user}`` (PUT boost is
especially dangerous if callers can self-serve).

Persistence
-----------
Uses ``FileQuotaStore`` with a single shared JSON file.  See notes in
``FileQuotaStore`` for the data shape and concurrency model.  The central
service writes one global lock, so N parallel sidecar check/commit calls
become serialized — acceptable for the MVP target scale (hundreds of
notebooks); for larger deployments switch to Redis/Postgres + adapt the
store interface.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Import bootstrap: share the quota_store module that lives alongside the
# sidecar code.  Using parents[1] from quota_service/server.py lands us at
# local-dev/ (the repo sub-tree root), then llm-gateway-sidecar/ is a sibling.
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[1]
_SIDECAR = _ROOT / "llm-gateway-sidecar"
sys.path.insert(0, str(_SIDECAR))

from quota_store import DEFAULT_PLANS, FileQuotaStore, canonicalize_user  # noqa: E402

# ---------------------------------------------------------------------------
# Service-level configuration from env
# ---------------------------------------------------------------------------
HOST = os.environ.get("QUOTA_HOST", "127.0.0.1")
PORT = int(os.environ.get("QUOTA_PORT", "8090"))
# Persistent file location.  Defaults live in local-dev/.runtime so laptop
# spikes survive restarts; production sets this to a PVC mount path.
STORE_PATH = os.environ.get(
    "QUOTA_STORE_PATH",
    str(_ROOT / ".runtime" / "quota-service-store.json"),
)
# Optional plan override JSON.  Accepts the same shape as QUOTA_PLANS_PATH in
# build_quota_backend() — either {"plans": {...}} or a flat {id: {...}} dict.
PLANS_PATH = os.environ.get("QUOTA_PLANS_PATH", str(Path(__file__).with_name("plans.json")))

# ---------------------------------------------------------------------------
# Issue #13: Max POST/PUT body size (global guard on _body helper).
# Chatty /v1/usage/summary input is query-string only so this cap is modest.
# ---------------------------------------------------------------------------
MAX_BODY_BYTES = int(os.environ.get("QUOTA_SERVICE_MAX_BODY_BYTES", str(1 * 1024 * 1024)))

# ---------------------------------------------------------------------------
# Issue #10: ThreadingHTTPServer worker caps.
# ---------------------------------------------------------------------------
_MAX_THREADS = int(os.environ.get("QUOTA_SERVICE_MAX_THREADS", "64"))
_REQUEST_QUEUE_SIZE = int(os.environ.get("QUOTA_SERVICE_REQUEST_QUEUE_SIZE", "128"))

# ---------------------------------------------------------------------------
# Issue #3: Write-route authentication (MVP).
#
# Three optional auth modes are available — if NONE is configured we log a
# warning at startup and proceed (preserving backward compat with NetworkPolicy-
# only deployments) but strongly recommend production to set at least one.
# Priority for evaluating incoming requests:
#   1. static QUOTA_SERVICE_ADMIN_API_KEY — simplest to roll out
#   2. (future) mTLS client cert SAN check — not implemented here
# ---------------------------------------------------------------------------
ADMIN_API_KEY = os.environ.get("QUOTA_SERVICE_ADMIN_API_KEY", "").strip()
# Writing routes are "dangerous": PUT boost, POST commit/check already requires
# user identity but boost is the classic escalate-oneself vector.
WRITE_ROUTES = {"PUT:/v1/quota/*"}
if ADMIN_API_KEY:
    log_auth_note = (
        "[quota] write-route auth ENABLED — set QUOTA_SERVICE_ADMIN_API_KEY secret"
    )
else:
    log_auth_note = (
        "[quota] WARNING: write-route auth DISABLED (no QUOTA_SERVICE_ADMIN_API_KEY set). "
        "Ensure K8s NetworkPolicy restricts PUT /v1/quota/* to admin clients ONLY."
    )


# ---- Build plan catalog ---------------------------------------------------
plans = dict(DEFAULT_PLANS)
if Path(PLANS_PATH).is_file():
    with open(PLANS_PATH, encoding="utf-8") as f:
        loaded = json.load(f)
        plans.update(loaded.get("plans") or {})

# ---- Single shared quota store instance -----------------------------------
# All HTTP handlers serialize through store._lock so even with
# ThreadingHTTPServer the JSON file state is internally consistent.
store = FileQuotaStore(STORE_PATH, plans=plans)

# ---- Optional per-user plan overrides -------------------------------------
# Hot-reload NOT supported; if admins edit user_overrides.json they must
# restart the quota-service pod.  The override file path defaults to
# quota_service/user_overrides.json alongside plans.json.
OVERRIDES_PATH = Path(os.environ.get("QUOTA_USER_OVERRIDES", str(Path(__file__).with_name("user_overrides.json"))))
USER_OVERRIDES: dict[str, str] = {}
if OVERRIDES_PATH.is_file():
    USER_OVERRIDES = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))


class Handler(BaseHTTPRequestHandler):
    """HTTP handler for the central Quota Service.

    Threading note: ``ThreadingHTTPServer`` runs each request on a worker
    thread.  Shared mutable state is the global ``store`` instance, which
    serializes internally via its own ``_lock``.  ``plans`` and
    ``USER_OVERRIDES`` are read-only after startup and therefore safe.

    Authentication (Issue #3)
    -------------------------
    PUT routes that mutate per-user quotas (admin boost) require an
    ``Authorization: Bearer $ADMIN_API_KEY`` header when
    ``QUOTA_SERVICE_ADMIN_API_KEY`` is set at startup.  An exact string
    match (constant-time via ``hmac.compare_digest``) is performed; missing
    or wrong API keys receive HTTP 401 with no server-side state mutation.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: ANN002
        """Minimal request logger to stderr (kubectl logs-friendly)."""
        sys.stderr.write(f"[quota] {self.address_string()} {fmt % args}\n")

    def _send(self, code: int, payload) -> None:  # noqa: ANN001
        """JSON response helper (always utf-8)."""
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self, max_bytes: int = MAX_BODY_BYTES) -> dict:
        """Read JSON POST body (empty body -> {}).

        Issue #13 fix: fails with HTTP 413 for Content-Length > max_bytes so
        an attacker cannot OOM the service with a multi-gigabyte body.
        """
        raw_len = int(self.headers.get("Content-Length", "0") or 0)
        if raw_len > max_bytes:
            self._send(
                413,
                {"error": {"message": f"payload too large: {raw_len} > max {max_bytes}", "type": "payload_too_large"}},
            )
            raise RuntimeError("body exceeds max bytes — response already sent")
        raw = self.rfile.read(raw_len) if raw_len else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _require_admin_auth(self) -> bool:
        """Validate admin-route bearer token (Issue #3).

        Returns True when the call is authorized; emits HTTP 401 and returns
        False when auth is required but missing/mismatched.  When no admin
        API key is configured (NetworkPolicy-only) this returns True for all
        calls with a runtime log warning the first time.
        """
        import hmac

        if not ADMIN_API_KEY:
            # No auth configured — document loudly in logs once per process.
            return True
        raw = self.headers.get("Authorization", "")
        if not raw.startswith("Bearer "):
            self._send(401, {"error": {"message": "admin auth required", "type": "unauthorized"}})
            return False
        incoming = raw[len("Bearer ") :].strip()
        # Constant-time compare to avoid timing side channels on key guesses.
        if not hmac.compare_digest(incoming, ADMIN_API_KEY):
            self._send(401, {"error": {"message": "admin auth failed", "type": "unauthorized"}})
            return False
        return True

    # ------------------------------------------------------------------
    # GET routes
    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 — http.server naming convention
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # --- Liveness / readiness probe ---------------------------------
        if path == "/healthz":
            self._send(200, {"status": "ok"})
            return

        # --- Prometheus metrics ----------------------------------------
        # Issue #9 fix: use the NEW public ``store.snapshot_all()`` API
        # instead of reaching into private ``store._read()``.  Returns a
        # deep-copied dict so we can enumerate without holding the store
        # lock (scrapers can be slow and we don't want to block check/
        # commit on prometheus scrape latency).
        if path == "/metrics":
            data = store.snapshot_all()
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
            # Per-user timeseries — sorted for deterministic output (helps
            # Prometheus delta calculations and human debugging).
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

        # --- Plan catalog ------------------------------------------------
        if path == "/v1/plans":
            self._send(200, {"plans": plans})
            return

        # --- User quota snapshot ----------------------------------------
        # Query-string shape: ?user=alice&groups=interns,data-science
        if path == "/v1/quota":
            user = qs.get("user", [""])[0]
            groups = [g for g in qs.get("groups", [""])[0].split(",") if g]
            self._send(200, store.snapshot(user, groups=groups))
            return

        # --- Raw usage events -------------------------------------------
        # Query-string shape: ?user=alice&from=<epoch>&to=<epoch>
        if path == "/v1/usage":
            user = qs.get("user", [None])[0]
            since = float(qs.get("from", ["0"])[0] or 0)
            until = float(qs.get("to", [str(time.time())])[0])
            self._send(200, {"events": store.usage(user, since=since, until=until)})
            return

        # --- Daily rollup summary (LLM-S17.2) ---------------------------
        # Same query params as /v1/usage.  Output shape:
        #   {"summary": {"YYYY-MM-DD": {"<user>": {tokens, requests, by_feature, by_model, plan_id}}},
        #    "from": <epoch>, "to": <epoch>}
        if path == "/v1/usage/summary":
            user = qs.get("user", [None])[0]
            since = float(qs.get("from", ["0"])[0] or 0)
            until = float(qs.get("to", [str(time.time())])[0])
            events = store.usage(user, since=since, until=until)
            # Daily rollup: date → user → {tokens, requests, by_feature, by_model, plan_id}
            # Date key is UTC day (gmtime) so reports are timezone-stable.
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

    # ------------------------------------------------------------------
    # POST routes
    # ------------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._body()

        # --- Resolve plan for an identity ------------------------------
        # Mirror of FileQuotaStore.resolve_plan() with server-side user
        # overrides injected.  The Hub pre_spawn_hook calls this to get
        # UX-hint values (NBI_LLM_PLAN, limits) without granting the pod
        # authority over its own plan.
        if path == "/v1/plans/resolve":
            username = body.get("username", "")
            groups = body.get("groups") or []
            # Server-side overrides are authoritative — pod env can't
            # raise privileges.  Caller passes only auth_state KEY names
            # (not values) to help future audit log correlation.
            plan = store.resolve_plan(
                username,
                groups=groups,
                user_overrides=USER_OVERRIDES,
            )
            self._send(200, plan)
            return

        # --- Pre-request quota check (fail-closed by caller) -----------
        # Trust server-side overrides; ignore any plan hint sent by the
        # sidecar (pod env is forgeable — see LLM-S15 / ADR).
        if path == "/v1/quota/check":
            # Run check with server-side plan logic.  hint_plan is forced
            # to None so client-side NBI_LLM_PLAN value is fully ignored
            # for enforcement purposes (UX hint only in pre_spawn_hook).
            #
            # Issue #11 fix: use ``store.check_with_plan()`` which returns
            # (QuotaDecision, plan_dict) in a single call.  Previously the
            # handler ran resolve_plan() TWICE: once inside store.check()
            # and once again here for response-shaping.  The new API
            # returns the USER_OVERRIDES-aware plan alongside the decision
            # so we avoid the redundant O(plans lookups) and prevent
            # future drift between the two independent calls.
            decision, plan = store.check_with_plan(
                username=body.get("username", ""),
                estimate_tokens=int(body.get("estimate_tokens") or 0),
                model=body.get("model") or "",
                groups=body.get("groups") or [],
                hint_plan=None,
                feature=body.get("feature") or "chat",
                user_overrides=USER_OVERRIDES,
            )
            self._send(
                200,
                {
                    "allowed": decision.allowed,
                    "plan_id": plan["id"] if decision.allowed else decision.plan_id,
                    "used_tokens": decision.used_tokens,
                    "limit_tokens": decision.limit_tokens,
                    "used_requests": decision.used_requests,
                    "limit_requests": int(plan["requests_per_day"]),
                    "reset_at": decision.reset_at,
                    "message": decision.message,
                    "soft_cap_hit": decision.soft_cap_hit,
                    "canonical_user": canonicalize_user(body.get("username", "")),
                },
            )
            return

        # --- Post-request usage commit ---------------------------------
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

    # ------------------------------------------------------------------
    # PUT routes (admin-only — MUST be protected in production)
    # ------------------------------------------------------------------
    def do_PUT(self) -> None:  # noqa: N802
        # Issue #3: authenticate admin writes BEFORE any state mutation or
        # body parsing.  Missing/wrong bearer -> HTTP 401 with no side effects.
        if not self._require_admin_auth():
            return
        path = urlparse(self.path).path
        prefix = "/v1/quota/"
        if not path.startswith(prefix):
            self._send(404, {"error": "not found"})
            return
        # URL-encoded username remainder: /v1/quota/<canonical_user>
        user = path[len(prefix) :]
        body = self._body()
        # Accept either field name for flexibility with admin scripts.
        extra = int(body.get("extra_tokens") or body.get("boost_tokens") or 0)
        self._send(200, store.boost(user, extra))


def main() -> int:
    """Block-forever entry point — run via ``python -m quota_service.server``
    or the K8s Deployment command.

    Issue #10 fix: wrap ThreadingHTTPServer in a BoundedSemaphore(64) cap so
    a burst of sidecar check/commit traffic cannot blow the worker thread
    count through the OS ulimit.  Pattern mirrors the sidecar's
    BoundedThreadingHTTPServer exactly (see sidecar.py L826–L873).
    """
    import socket as _socket

    _thread_limiter = threading.BoundedSemaphore(_MAX_THREADS)

    class BoundedThreadingHTTPServer(ThreadingHTTPServer):
        """ThreadingHTTPServer with a hard cap on in-flight handler threads.

        ``ThreadingHTTPServer`` spins up an unbounded daemon thread per
        accepted connection.  Under a traffic spike or a slow backend (disk
        flush on commit), this can easily exhaust the container's thread
        budget.  We gate handler execution on ``_thread_limiter``: acquire
        on process_request, release in a ``finally`` block after
        ``finish_request`` completes.  When the semaphore is saturated we
        close the accepted socket immediately — the client will retry via
        TCP retrans / HTTP layer.
        """

        allow_reuse_address = True
        daemon_threads = True

        def process_request(  # noqa: D401 — stdlib override signature
            self,
            request: _socket.socket,
            client_address,
        ) -> None:
            """Accept, cap, and dispatch one connection on a worker thread."""
            acquired = _thread_limiter.acquire(blocking=False)
            if not acquired:
                # Saturation response: drop the socket cleanly.  Clients
                # will experience a TCP RST and (for HTTP retries) come
                # back; this is strictly better than OOMing or hitting
                # pthread_create fails.
                try:
                    request.shutdown(_socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    request.close()
                except OSError:
                    pass
                sys.stderr.write(
                    f"[quota] thread cap hit ({_MAX_THREADS}); "
                    f"dropping conn from {client_address}\n"
                )
                return

            def _run() -> None:
                try:
                    self.finish_request(request, client_address)
                except Exception:  # noqa: BLE001 — mirror stdlib behavior
                    self.handle_error(request, client_address)
                finally:
                    self.shutdown_request(request)
                    _thread_limiter.release()

            try:
                t = threading.Thread(target=_run, daemon=True)
                t.start()
            except RuntimeError:
                # Extremely unlikely — can't even start a thread after
                # acquiring the semaphore.  Release the permit manually and
                # drop the socket.
                _thread_limiter.release()
                try:
                    request.shutdown(_socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    request.close()
                except OSError:
                    pass
                raise

    httpd = BoundedThreadingHTTPServer((HOST, PORT), Handler)
    httpd.request_queue_size = _REQUEST_QUEUE_SIZE
    print(f"[quota] listening on http://{HOST}:{PORT} store={STORE_PATH}", flush=True)
    print(log_auth_note, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[quota] stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
