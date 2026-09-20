"""
nbi_reload_client.py — Trigger NBI's reload-config endpoint from the sidecar.

The Problem
-----------
The sidecar and the notebook server share a pod network namespace, so
``127.0.0.1`` reaches the notebook container's Tornado process.  But:

  * The jupyter server listens on a DYNAMIC port (or a fixed one from the
    Z2JK chart, but we can't rely on that).
  * Auth requires the jupyter server token, which is written to
    ``~/.local/share/jupyter/runtime/jpserver-*.json`` *after* the notebook
    process has started.
  * The NBI extension registers routes during initialize_handlers, which
    happens ~5–15 seconds after the jupyter server process itself binds
    the socket.

This means a naive startup-time HTTP call will almost always race and
fail.  This client handles all three failure modes with bounded retries
and clear error reporting.

Protocol (duck type)
--------------------
Implements the ``NBIReloadClient`` Protocol from scheduler.py::

    def reload(self, *, broadcast: bool = True) -> tuple[bool, dict]: ...

Return values
-------------
Success (NBI endpoint returned 2xx):
    ``(True,  {"status": "ok", "http_status": 200, "body": <parsed json or {}>})``

Transient failures (connect refused, route 404 because NBI not yet
registered, token file missing, etc):
    ``(False, {"status": "transient", "http_status": 0|404|5xx, "reason": "…"})``

Permanent auth failure (token exists but 403 every time):
    ``(False, {"status": "forbidden", "http_status": 403, "reason": "…"})``

The scheduler NEVER raises to its caller — that's the whole point of
having this client return structured tuples.  Exceptions inside this
module are caught and converted to ``(False, {…transient…})`` so the
scheduler's FS-poller fallback is always available as a safety net.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, MutableMapping, Optional, Tuple

from .redaction import redact_secrets

log = logging.getLogger("nbi_auth_sidecar.reload_client")

# ---------------------------------------------------------------------------
# 1. Defaults — overridable via env for tests & unusual deployments.
# ---------------------------------------------------------------------------

#: Directory that holds ``jpserver-<pid>.json`` files.  Respects the
#: official ``JUPYTER_RUNTIME_DIR`` env var; falls back to the
#: well-known XDG-ish location used by stock Jupyter.
DEFAULT_RUNTIME_DIR = os.environ.get(
    "JUPYTER_RUNTIME_DIR",
    os.path.join(os.path.expanduser("~"), ".local", "share", "jupyter", "runtime"),
)
#: How long to keep polling for the runtime JSON before giving up for
#: *this* call.  ``__main__`` calls reload(broadcast=True) as part of
#: the bootstrap sync block; we allow the full bootstrap budget there.
DEFAULT_DISCOVERY_TIMEOUT_SEC = int(
    os.environ.get("NBI_RELOAD_DISCOVERY_TIMEOUT_SEC", "60")
)
#: Sleep between discovery retries (file exists? port open? route 200?).
DEFAULT_DISCOVERY_INTERVAL_SEC = float(
    os.environ.get("NBI_RELOAD_DISCOVERY_INTERVAL_SEC", "2.0")
)
#: HTTP connect/read timeout for each individual POST attempt.  The
#: jupyter server is localhost so 5 seconds is generous; it also bounds
#: a Tornado event-loop stall from wedging the sidecar.
DEFAULT_HTTP_TIMEOUT_SEC = float(
    os.environ.get("NBI_RELOAD_HTTP_TIMEOUT_SEC", "5.0")
)
#: Total POST retry attempts (per reload() call) after discovery succeeds.
DEFAULT_POST_ATTEMPTS = int(os.environ.get("NBI_RELOAD_POST_ATTEMPTS", "3"))
#: POST retry backoff (plain linear — endpoint is on loopback so jitter
#: is unnecessary; 2-4-8s is plenty).
DEFAULT_POST_BACKOFF_SEC = float(
    os.environ.get("NBI_RELOAD_POST_BACKOFF_SEC", "2.0")
)


# ---------------------------------------------------------------------------
# 2. Runtime descriptor — parsed copy of ``jpserver-*.json``.  We only
#    need port + token; everything else is informational.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JupyterRuntimeInfo:
    """Parsed subset of a ``~/.local/share/jupyter/runtime/jpserver-*.json``."""

    port: int
    token: str
    #: Full filesystem path of the source JSON — useful when multiple
    #: jupyter servers run inside one pod (rare, but we handle it by
    #: picking the file with the most recent mtime).
    source_path: str
    #: Base URL the server is listening under (Z2JK often puts servers
    #: under ``/user/<name>/``).  Defaults to ``/``.
    base_url: str = "/"

    @property
    def endpoint_url(self) -> str:
        """Fully-qualified URL for the NBI reload-config handler."""
        # Normalise base_url so we never produce "//notebook-intelligence/…".
        base = self.base_url.rstrip("/") + "/"
        path = "notebook-intelligence/reload-config"
        return f"http://127.0.0.1:{self.port}{base}{path}"


# ---------------------------------------------------------------------------
# 3. Jupyter runtime discovery — robust to slow process startup.
# ---------------------------------------------------------------------------


def _find_latest_runtime_file(runtime_dir: str) -> Optional[str]:
    """Return the absolute path of the most recently-written jpserver JSON.

    Returns ``None`` when the directory does not exist or contains no
    matching files.  Uses mtime (not ctime) so ``touch``-style heartbeats
    by the notebook server don't skew the result — the most recently
    *written* file corresponds to the most recently-started server
    instance, which is the one we want in multi-launch scenarios.
    """
    if not os.path.isdir(runtime_dir):
        return None
    pattern = os.path.join(runtime_dir, "jpserver-*.json")
    candidates = glob.glob(pattern)
    if not candidates:
        return None
    # Sort by mtime descending.
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]


def _parse_runtime_file(path: str) -> Optional[JupyterRuntimeInfo]:
    """Parse one jpserver JSON file.  Returns ``None`` on any parse problem."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not parse runtime file %s: %s", path, redact_secrets(str(exc)))
        return None
    port = data.get("port") or data.get("control_port")
    token = data.get("token")
    if not isinstance(port, int) or port <= 0 or port > 65535:
        log.warning("Runtime file %s missing valid 'port' field: %s", path, port)
        return None
    if not isinstance(token, str):
        # Token is sometimes an empty string when auth is disabled (local-dev).
        token = ""
    base_url = data.get("base_url") or "/"
    if not isinstance(base_url, str) or not base_url.startswith("/"):
        base_url = "/"
    return JupyterRuntimeInfo(
        port=port,
        token=token,
        source_path=path,
        base_url=base_url,
    )


def discover_jupyter_runtime(
    *,
    runtime_dir: str = DEFAULT_RUNTIME_DIR,
    timeout_sec: int = DEFAULT_DISCOVERY_TIMEOUT_SEC,
    interval_sec: float = DEFAULT_DISCOVERY_INTERVAL_SEC,
    _time_fn=time.time,
    _sleep_fn=time.sleep,
) -> Optional[JupyterRuntimeInfo]:
    """Poll the runtime directory until a parseable jpserver JSON appears.

    Returns ``None`` when the timeout fires — caller must degrade to
    FS-poller only.  This function is used by *every* reload() call,
    which gives us resilience against the notebook container restarting
    its Python process (e.g. ``jupyter server stop; jupyter lab``) while
    the pod keeps running.
    """
    deadline = _time_fn() + timeout_sec
    last_path: Optional[str] = None
    while _time_fn() < deadline:
        path = _find_latest_runtime_file(runtime_dir)
        if path != last_path and path is not None:
            info = _parse_runtime_file(path)
            if info is not None:
                log.info(
                    "Discovered jupyter runtime on port %d base_url=%s (file=%s, age=%.1fs)",
                    info.port,
                    info.base_url,
                    os.path.basename(info.source_path),
                    _time_fn() - os.path.getmtime(info.source_path),
                )
                return info
            last_path = path
        _sleep_fn(interval_sec)
    log.warning(
        "Timed out after %ds waiting for jpserver-*.json in %s (file_found=%s).",
        timeout_sec,
        runtime_dir,
        _find_latest_runtime_file(runtime_dir) is not None,
    )
    return None


# ---------------------------------------------------------------------------
# 4. HTTP POST wrapper — stdlib-only, never raises for transport errors.
# ---------------------------------------------------------------------------


def _post_reload_once(
    info: JupyterRuntimeInfo,
    *,
    broadcast: bool,
    timeout_sec: float,
) -> Tuple[bool, int, str, Any]:
    """Execute ONE HTTP POST.  Returns ``(ok, http_status, reason, body)``.

    ``http_status`` is 0 for pure transport errors (no TCP ACK, DNS,
    etc).  ``body`` is either the parsed JSON dict or ``None`` when the
    response was not valid JSON.
    """
    body_bytes = json.dumps({"broadcast": bool(broadcast)}).encode("utf-8")
    req = urllib.request.Request(
        info.endpoint_url,
        data=body_bytes,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Jupyter server auth: two mechanisms are supported; we send both:
            #   1. Query-param ``?token=…`` (classic Notebook Server style)
            #   2. ``Authorization: token <value>`` header (Jupyter Server style)
            "Authorization": f"token {info.token}",
        },
    )
    # Append ``?token=`` query param as a safe redundant fallback.
    # (urllib does not mutate the original URL when we do this manually.)
    sep = "&" if "?" in info.endpoint_url else "?"
    final_url = f"{info.endpoint_url}{sep}token={urllib.parse.quote(info.token, safe='')}"
    # Re-create Request with the enriched URL because urllib doesn't let
    # you mutate full_url after construction.
    req = urllib.request.Request(
        final_url,
        data=body_bytes,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"token {info.token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            status = resp.getcode()
            raw = resp.read()
            parsed_body: Any = None
            try:
                if raw:
                    parsed_body = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                parsed_body = None
            ok = 200 <= status < 300
            return ok, status, "ok" if ok else f"http_{status}", parsed_body
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read()
            parsed_body = json.loads(raw.decode("utf-8", errors="replace")) if raw else None
        except Exception:
            parsed_body = None
        # 404 in the first ~60s after pod start means the NBI extension
        # hasn't registered its routes yet — that's a transient condition.
        return False, status, f"http_{status}", parsed_body
    except urllib.error.URLError as exc:
        # Connection refused, DNS, no route — transient until proven otherwise.
        return False, 0, f"url_error:{type(exc.reason).__name__}", None
    except socket.timeout:
        return False, 0, "connect_timeout", None
    except TimeoutError:
        return False, 0, "read_timeout", None
    except Exception as exc:  # noqa: BLE001
        # Defensive: convert anything else into a structured transient failure.
        return False, 0, f"unexpected:{type(exc).__name__}", None


# ---------------------------------------------------------------------------
# 5. Public client class — implements scheduler.NBIReloadClient Protocol.
# ---------------------------------------------------------------------------


class DefaultNBIReloadClient:
    """Production implementation.  All behaviour tunable via constructor kwargs."""

    def __init__(
        self,
        *,
        runtime_dir: Optional[str] = None,
        discovery_timeout_sec: Optional[int] = None,
        discovery_interval_sec: Optional[float] = None,
        http_timeout_sec: Optional[float] = None,
        post_attempts: Optional[int] = None,
        post_backoff_sec: Optional[float] = None,
    ) -> None:
        self._runtime_dir = runtime_dir or DEFAULT_RUNTIME_DIR
        self._discovery_timeout_sec = discovery_timeout_sec or DEFAULT_DISCOVERY_TIMEOUT_SEC
        self._discovery_interval_sec = discovery_interval_sec or DEFAULT_DISCOVERY_INTERVAL_SEC
        self._http_timeout_sec = http_timeout_sec or DEFAULT_HTTP_TIMEOUT_SEC
        self._post_attempts = post_attempts or DEFAULT_POST_ATTEMPTS
        self._post_backoff_sec = post_backoff_sec or DEFAULT_POST_BACKOFF_SEC
        # Cache the last successfully-parsed runtime info so we skip
        # directory scanning on the hot path (reload() can be called
        # every 5m for the lifetime of the pod).
        self._cached_runtime: Optional[JupyterRuntimeInfo] = None

    def reload(self, *, broadcast: bool = True) -> tuple[bool, dict]:
        """Protocol entry point.  Never raises."""
        # ---- Discovery --------------------------------------------------
        info = self._cached_runtime
        if info is None or not os.path.exists(info.source_path):
            info = discover_jupyter_runtime(
                runtime_dir=self._runtime_dir,
                timeout_sec=self._discovery_timeout_sec,
                interval_sec=self._discovery_interval_sec,
            )
            if info is None:
                detail = {
                    "status": "transient",
                    "http_status": 0,
                    "reason": "runtime_not_found",
                    "runtime_dir": self._runtime_dir,
                }
                return False, detail
            self._cached_runtime = info
        else:
            # Fast path: cached info — but re-read the token in case the
            # notebook server restarted and rotated its own token file
            # in place (rare, but the cost of one small read is ~0).
            refreshed = _parse_runtime_file(info.source_path)
            if refreshed is not None:
                info = refreshed
                self._cached_runtime = info
        # ---- POST with retries -----------------------------------------
        last_status = 0
        last_reason = "no_attempt"
        last_body: Any = None
        for attempt in range(1, self._post_attempts + 1):
            ok, http_status, reason, body = _post_reload_once(
                info,
                broadcast=broadcast,
                timeout_sec=self._http_timeout_sec,
            )
            last_status = http_status
            last_reason = reason
            last_body = body
            if ok:
                return True, {
                    "status": "ok",
                    "http_status": http_status,
                    "attempt": attempt,
                    "body": body if isinstance(body, Mapping) else {},
                    "broadcast": broadcast,
                }
            # Classification: 401/403 are permanent-ish (token is wrong),
            # 404 is transient (extension not registered yet, retry helps),
            # everything else is transient.
            if http_status in {401, 403}:
                log.warning(
                    "Reload POST attempt %d/%d got %d (forbidden).  Will not retry POST this call.",
                    attempt,
                    self._post_attempts,
                    http_status,
                )
                return False, {
                    "status": "forbidden",
                    "http_status": http_status,
                    "reason": f"auth_{reason}",
                    "attempt": attempt,
                    "endpoint": info.endpoint_url.replace(info.token, "[REDACTED]"),
                }
            # Retry with linear backoff 2-4-8s.
            backoff = self._post_backoff_sec * attempt
            log.info(
                "Reload POST attempt %d/%d failed (http=%d reason=%s).  Retrying in %.1fs…",
                attempt,
                self._post_attempts,
                http_status,
                redact_secrets(reason),
                backoff,
            )
            time.sleep(backoff)
        # All attempts exhausted.
        detail: MutableMapping[str, Any] = {
            "status": "transient",
            "http_status": last_status,
            "reason": last_reason,
            "attempts": self._post_attempts,
        }
        if isinstance(last_body, Mapping):
            detail["last_body"] = last_body
        return False, detail
