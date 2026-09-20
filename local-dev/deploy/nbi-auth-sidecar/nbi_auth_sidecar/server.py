"""
server.py — Sidecar local HTTP surface (127.0.0.1 ONLY).

Endpoints
---------
GET  /healthz
    Always 200 while the process is alive.  Liveness probe target.
    *Never* returns non-200 — liveness failures crash-loop the container and
    we'd rather keep the sidecar alive with a stale token than throw
    away the user's running notebook.  Pod restarts kill the kernel.

GET  /ready
    200 iff scheduler.ready reports True (valid cached token AND mint is not
    in the consecutive-failure death spiral).  Readiness probe target.  When
    NotReady the pod disappears from Service endpoints — which doesn't really
    matter for a loopback-only server, but operators querying metrics will see
    "N pods Ready" and alert correctly.

GET  /metrics
    Prometheus text exposition format.  Counters/gauges for successful
    refresh count, attempts, TTL remaining, consecutive failures, JVM
    subprocess seconds (soon), etc.  Text exposition only, no OpenMetrics —
    Prometheus scrapes both happily.

POST /rotate-self
    Force an out-of-band token refresh.  Calls
    ``scheduler.trigger_refresh_now()`` and returns 202 Accepted
    immediately — the actual cycle runs in the scheduler daemon thread.  Body
    is ignored (optionally accepts JSON with ``{"reason": "..."}`` for logs).
    Auth is loopback-only so no extra auth layer required; anything that can
    reach 127.0.0.1 inside the pod is already the user or K8s kubelet.

Hard rules
----------
* Bind host MUST be ``127.0.0.1``.  ``0.0.0.0``, ``::``, or any other
  wildcard raise :class:`ValueError` at construction time.  This is a
  SECURITY boundary — the sidecar carries bearer tokens and must never be
  reachable from outside the pod network namespace.
* Python stdlib only — ``http.server.ThreadingHTTPServer`` plus
  ``BaseHTTPRequestHandler``.  No FastAPI, no aiohttp, no pip installs.
* Server runs on the MAIN thread via :meth:`serve_forever` so signal
  handlers work correctly (``ThreadingHTTPServer`` is fork-safe for our
  limited use case).
* All endpoints return short JSON bodies where applicable; ``/metrics`` returns
  ``text/plain; version=0.0.4`` per Prom convention.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping, MutableMapping, Optional, Tuple

from .scheduler import TokenScheduler  # type: ignore  # noqa: F401  (used in types)

log = logging.getLogger("nbi_auth_sidecar.server")

# ---------------------------------------------------------------------------
# 1. Defaults — env overridable.
# ---------------------------------------------------------------------------

DEFAULT_HOST = os.environ.get("NBI_SIDECAR_HTTP_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("NBI_SIDECAR_HTTP_PORT", "18090"))
#: Maximum number of concurrent handler threads.  K8s probe + user-initiated
#: rotate-self — 8 is more than enough; ThreadingHTTPServer's default
#: thread-per-connection can otherwise consume all fds.
DEFAULT_MAX_THREADS = int(os.environ.get("NBI_SIDECAR_HTTP_MAX_THREADS", "8"))

#: Prometheus text exposition newline is 0.0.4 plain text.
PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


# ---------------------------------------------------------------------------
# 2. Thread-capped HTTP server — prevent fd exhaustion.
# ---------------------------------------------------------------------------


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` subclass with a bounded worker-thread ceiling.

    Vanilla ``ThreadingHTTPServer`` spawns one ``Thread`` per connection
    and never reaps them; under a flood this hits ulimit -n (typically
    1024) and kills the container.  We track the active thread count with
    a ``threading.Semaphore`` and close the socket with ``Connection: close``
    after every handler so idle threads exit promptly.
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *args: Any, max_threads: int = DEFAULT_MAX_THREADS, **kwargs: Any) -> None:
        self._thread_sem = threading.Semaphore(max_threads)
        self._active_count_lock = threading.Lock()
        self._active_count = 0
        super().__init__(*args, **kwargs)

    # -- threading mixin overrides ---------------------------------------------------
    def process_request(self, request, client_address):  # pragma: no cover - trivial
        # Acquire slot; if we can't, close the connection with 503 in the
        # accept path rather than queuing forever.
        acquired = self._thread_sem.acquire(blocking=False)
        if not acquired:
            try:
                # Best-effort 503 via raw send — no TLS so close is fine.
                request.sendall(b"HTTP/1.1 503 Too Many Connections\r\nConnection: close\r\n\r\n")
            except Exception:
                pass
            try:
                request.close()
            except Exception:
                pass
            return
        # Semaphore acquired.  Increment active count BEFORE delegating; the
        # handler thread decrements it in process_request_thread's finally.
        with self._active_count_lock:
            self._active_count += 1
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):  # type: ignore[override]  # noqa: D401
        """Mirror BaseHTTP mixin — release semaphore after handler done."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._thread_sem.release()
            with self._active_count_lock:
                self._active_count -= 1

    @property
    def active_threads(self) -> int:
        with self._active_count_lock:
            return self._active_count


# ---------------------------------------------------------------------------
# 3. Request handler.
# ---------------------------------------------------------------------------


class SidecarRequestHandler(BaseHTTPRequestHandler):
    """Route dispatch for the four sidecar HTTP surface.

    The parent :class:`BaseHTTPRequestHandler` API is a little clunky — we
    override ``do_GET`` / ``do_HEAD`` / ``do_POST`` and short-circuit on
    known paths.  Unknown paths return 404 with a JSON body.
    """

    # ------------------------------------------------------------------
    # BaseHTTPRequestHandler logging is chatty by default; pipe to our
    # structured logger with level tuning.
    # ------------------------------------------------------------------
    server_version = "NBIAuthSidecar/1.0"
    sys_version = ""  # Don't leak Python version banner on error pages

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - match parent signature
        # Use info for 2xx/3xx; warn for 4xx; error for 5xx.
        code = args[1] if len(args) >= 2 else "000"
        try:
            code_int = int(str(code).split()[0] if " " in str(code) else code)
        except (TypeError, ValueError):
            code_int = 0
        msg = "%s - %s" % (self.address_string(), format % args)
        if 500 <= code_int < 600:
            log.error(msg)
        elif 400 <= code_int < 500:
            log.warning(msg)
        else:
            log.info(msg)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def _send_json(self, status: int, payload: Mapping[str, Any], *, headers: Optional[Mapping[str, str]] = None) -> None:
        body = json.dumps(dict(payload)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str, content_type: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def do_HEAD(self) -> None:
        # Same routing table as GET but without writing body.
        path, _sep, _query = self.path.partition("?")
        if path in {"/healthz", "/ready", "/metrics"}:
            # HEAD -> 200 + headers only.
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_GET(self) -> None:
        path, _sep, query = self.path.partition("?")
        if path == "/healthz":
            return self._handle_healthz()
        if path == "/ready":
            return self._handle_ready()
        if path == "/metrics":
            return self._handle_metrics()
        return self._send_json(404, {"error": "not_found", "path": path})

    def do_POST(self) -> None:
        path, _sep, query = self.path.partition("?")
        if path == "/rotate-self":
            return self._handle_rotate_self()
        return self._send_json(404, {"error": "not_found", "path": path})

    # ------------------------------------------------------------------
    # Individual handlers
    # ------------------------------------------------------------------

    def _get_scheduler(self) -> TokenScheduler:
        """Fish the scheduler off the server instance.

        We stow the scheduler on the server's ``scheduler`` attribute in
        ``serve``.  The handler class is constructed per request, so we
        access it via ``self.server``.
        """
        sched = getattr(self.server, "scheduler", None)
        if sched is None:
            raise RuntimeError("Server missing 'scheduler' attribute — wiring bug in __main__")
        return sched  # type: ignore[no-any-return]

    def _handle_healthz(self) -> None:
        """Liveness probe.  Always 200 while Python is still interpreting code.

        Extra payload includes the process start_time and pid for debugging — purely
        informational, since the liveness probe only checks status code.
        """
        sched = self._get_scheduler()
        state = sched.snapshot_state()
        payload: MutableMapping[str, Any] = {
            "status": "ok",
            "pid": os.getpid(),
            "now": time.time(),
            "last_refresh_ts": state.last_refresh_ts,
            "last_refresh_reason": state.last_refresh_reason,
            "token_ttl_seconds": max(0, int(state.token_expires_at - time.time())),
            "consecutive_mint_failures": state.consecutive_mint_failures,
            "successful_refresh_count": state.successful_refresh_count,
            "attempted_refresh_count": state.attempted_refresh_count,
        }
        self._send_json(200, payload)

    def _handle_ready(self) -> None:
        """Readiness probe.  200 when scheduler.ready, 503 otherwise.
        Same payload as /healthz so operators can curl it interactively.
        """
        sched = self._get_scheduler()
        state = sched.snapshot_state()
        ready = sched.ready
        payload: MutableMapping[str, Any] = {
            "status": "ready" if ready else "not_ready",
            "pid": os.getpid(),
            "now": time.time(),
            "last_refresh_ts": state.last_refresh_ts,
            "last_refresh_reason": state.last_refresh_reason,
            "token_ttl_seconds": max(0, int(state.token_expires_at - time.time())),
            "consecutive_mint_failures": state.consecutive_mint_failures,
            "successful_refresh_count": state.successful_refresh_count,
            "attempted_refresh_count": state.attempted_refresh_count,
        }
        self._send_json(200 if ready else 503, payload)

    def _handle_metrics(self) -> None:
        """Prometheus exposition — 8 gauge/counter families max.

        Names follow ``nbi_auth_sidecar_<metric>`` convention so they
        sit nicely next to ``nbi_quota_*`` and ``nbi_llm_*`` metrics
        already produced by the other two sidecars.
        """
        sched = self._get_scheduler()
        state = sched.snapshot_state()
        server_obj: BoundedThreadingHTTPServer = self.server  # type: ignore[assignment]
        now = time.time()
        ttl = max(0, int(state.token_expires_at - now))
        lines = [
            "# HELP nbi_auth_sidecar_info Constant 1, labels carry sidecar metadata.",
            "# TYPE nbi_auth_sidecar_info gauge",
            'nbi_auth_sidecar_info{version="1.0.0"} 1',
            "# HELP nbi_auth_sidecar_refresh_success_total Successful token refresh cycles (successes).",
            "# TYPE nbi_auth_sidecar_refresh_success_total counter",
            f"nbi_auth_sidecar_refresh_success_total {state.successful_refresh_count}",
            "# HELP nbi_auth_sidecar_refresh_attempt_total All token refresh attempts (success + failure).",
            "# TYPE nbi_auth_sidecar_refresh_attempt_total counter",
            f"nbi_auth_sidecar_refresh_attempt_total {state.attempted_refresh_count}",
            "# HELP nbi_auth_sidecar_consecutive_mint_failures Consecutive failed refresh failures since last success.",
            "# TYPE nbi_auth_sidecar_consecutive_mint_failures gauge",
            f"nbi_auth_sidecar_consecutive_mint_failures {state.consecutive_mint_failures}",
            "# HELP nbi_auth_sidecar_token_ttl_seconds Seconds remaining on the current cached token (0 when none).",
            "# TYPE nbi_auth_sidecar_token_ttl_seconds gauge",
            f"nbi_auth_sidecar_token_ttl_seconds {ttl}",
            "# HELP nbi_auth_sidecar_last_refresh_ts_seconds Unix timestamp of last successful refresh.",
            "# TYPE nbi_auth_sidecar_last_refresh_ts_seconds gauge",
            f"nbi_auth_sidecar_last_refresh_ts_seconds {state.last_refresh_ts}",
            "# HELP nbi_auth_sidecar_ready 1 if the scheduler reports ready, 0 otherwise.",
            "# TYPE nbi_auth_sidecar_ready gauge",
            f"nbi_auth_sidecar_ready {1 if sched.ready else 0}",
            "# HELP nbi_auth_sidecar_http_active_requests In-flight HTTP handler threads.",
            "# TYPE nbi_auth_sidecar_http_active_requests gauge",
            f"nbi_auth_sidecar_http_active_requests {server_obj.active_threads}",
        ]
        text = "\n".join(lines) + "\n"
        self._send_text(200, text, PROMETHEUS_CONTENT_TYPE)

    def _handle_rotate_self(self) -> None:
        """POST /rotate-self — trigger out-of-band refresh.

        Accepts optional JSON body with a ``reason`` field that is logged
        (useful for audit trail: "kubectl exec rotate-self manual rotation after IdP cert roll").
        """
        sched = self._get_scheduler()
        # Drain the body to avoid a hung connection.
        raw_len = self.headers.get("Content-Length", "0") or "0"
        reason_log: str = "http_rotate_self_endpoint"
        try:
            length = max(0, min(65536, int(raw_len)))
            if length > 0:
                body = self.rfile.read(length)
                try:
                    parsed = json.loads(body.decode("utf-8", errors="replace"))
                    if isinstance(parsed, dict) and "reason" in parsed and isinstance(parsed["reason"], str):
                        reason_log = parsed["reason"][:200]
                except json.JSONDecodeError:
                    pass
        except (ValueError, OSError):
            pass
        sched.trigger_refresh_now()
        log.info("rotate-self accepted from %s: reason=%s", self.address_string(), reason_log)
        state = sched.snapshot_state()
        self._send_json(
            202,
            {
                "status": "accepted",
                "scheduled_at": time.time(),
                "reason": reason_log,
                "successful_refresh_count": state.successful_refresh_count,
            },
        )


# ---------------------------------------------------------------------------
# 4. Server construction + run wrapper used by __main__.
# ---------------------------------------------------------------------------


def _assert_host_is_loopback(host: str) -> None:
    """Raise :class:`ValueError` if ``host`` is not a strict loopback address.

    ``127.0.0.1``, ``127.0.0.2``, ``::1`` are OK.
    ``0.0.0.0``, empty string, the pod IP, hostname — all raise.  The test is
    host-string based because the :class:`socket.getaddrinfo` can report any
    hostname to a non-loopback route — we err on the side of caution and
    only accept literal addresses.
    """
    if host in ("127.0.0.1", "::1", "localhost"):
        return
    # Any 127/8 is technically loopback — allow for flexibility.
    if host.startswith("127."):
        try:
            socket.inet_aton(host)  # valid dotted quad
            return
        except OSError:
            pass
    raise ValueError(
        f"NBI_SIDECAR_HTTP_HOST={host!r} is not a loopback address. "
        "For security the sidecar HTTP surface MUST bind to 127.0.0.1 or ::1 ONLY. "
        "Set NBI_SIDECAR_HTTP_HOST=127.0.0.1 explicitly to continue."
    )


def build_server(
    scheduler: TokenScheduler,
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    max_threads: int = DEFAULT_MAX_THREADS,
) -> BoundedThreadingHTTPServer:
    """Construct and return a BoundedThreadingHTTPServer wired to ``scheduler``.

    Raises ``ValueError`` on illegal bind host (security check).
    """
    bind_host = (host or DEFAULT_HOST).strip() or DEFAULT_HOST
    bind_port = port or DEFAULT_PORT
    _assert_host_is_loopback(bind_host)
    server = BoundedThreadingHTTPServer(
        (bind_host, bind_port),
        SidecarRequestHandler,
        max_threads=max_threads,
    )
    # Stash scheduler for handlers.
    server.scheduler = scheduler  # type: ignore[attr-defined]
    log.info(
        "NBI auth sidecar HTTP server constructed — bind=%s:%d max_threads=%d pid=%d",
        bind_host,
        bind_port,
        max_threads,
        os.getpid(),
    )
    return server


def serve_forever(
    scheduler: TokenScheduler,
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    max_threads: int = DEFAULT_MAX_THREADS,
) -> None:
    """Block forever serving traffic on the HTTP until KeyboardInterrupt/SIGTERM."""
    server = build_server(scheduler, host=host, port=port, max_threads=max_threads)
    try:
        log.info("Starting serve_forever — press Ctrl+C (or send SIGTERM) to exit.")
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — exiting serve_forever().")
    finally:
        try:
            server.server_close()
        except Exception:
            pass
        log.info("HTTP server closed cleanly.")
