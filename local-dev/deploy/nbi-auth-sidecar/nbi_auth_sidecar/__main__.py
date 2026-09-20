"""
__main__.py — NBI auth sidecar entrypoint: ``python -m nbi_auth_sidecar``.

Wiring order (non-negotiable because the signal handler must be installed
before any threads start, and the bootstrap block must run synchronously
before the daemon scheduler thread fires):

    1. Configure logging (structured one-liners; log level from env).
    2. Parse env into an Options dataclass with validated types.
    3. Construct minter → reload_client → scheduler in that order.
    4. Install SIGTERM + SIGINT handlers that call scheduler.stop() AND
       signal the HTTP server's ``shutdown()`` via a background thread so
       signal delivery never deadlocks.
    5. SYNCHRONOUS bootstrap block: ``scheduler.bootstrap()`` with the
       configured 90s budget.  On permanent failure: ``sys.exit(1)`` so
       K8s marks the container as crash-looping.
    6. Spawn scheduler daemon thread with ``run_forever()`` as target.
    7. Call ``server.serve_forever()`` on the MAIN thread so Python's
       signal semantics work (signals arrive on the main thread only).

Env config table
----------------
NBI_SIDECAR_MINTER = "jar" | "mock"        (default: jar, require real JKS creds in prod)
NBI_SIDECAR_HTTP_HOST / PORT / MAX_THREADS (default 127.0.0.1 / 18090 / 8)
NBI_CHAT_MODEL_PROVIDER / ID               (e.g. openai_compatible / databricks/gdp-gpt4o)
NBI_INLINE_COMPLETION_MODEL_PROVIDER / ID  (fallback = chat model ID if missing)
NBI_CLAUDE_CHAT_MODEL / INLINE             (optional, "" = not set)
ANTHROPIC_API_KEY = ""                     (unused — minted token is written here)
ANTHROPIC_BASE_URL = "https://…/v1"        (AI Factory gateway)
NBI_REFRESH_BEFORE_EXP_SEC = 300           (T-5m rule)
NBI_FORCE_REFRESH_INTERVAL_SEC = 0         (0 = disabled)
NBI_BOOTSTRAP_TIMEOUT_SEC = 90
NBI_RUNTIME_DIR = auto (JUPYTER_RUNTIME_DIR respected by reload_client)
NBI_LOG_LEVEL = INFO | DEBUG | WARNING     (default INFO)

All user identity (username, groups) comes from the notebook container's
environment — the sidecar doesn't care because JKS credentials are shared
per-namespace in K8s Secret ``nbi-llm-auth``.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .mint import build_token_minter
from .nbi_reload_client import DefaultNBIReloadClient
from .redaction import redact_secrets
from .scheduler import RefreshParams, TokenScheduler, build_scheduler_from_env
from .server import build_server, serve_forever

# ---------------------------------------------------------------------------
# 1. Logging — configure ASAP so even import-time errors are surfaced.
# ---------------------------------------------------------------------------


def _configure_logging() -> None:
    """Configure the root logger with a compact one-line format.

    Uses ``%(name)s`` so each sub-module (mint, scheduler, …) can be
    grepped in a single aggregated pod log stream.  Default level INFO;
    set ``NBI_LOG_LEVEL=DEBUG`` in the Helm env for verbose mint output.
    """
    level_name = os.environ.get("NBI_LOG_LEVEL", "INFO").upper()
    numeric = getattr(logging, level_name, logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric)


_configure_logging()
log = logging.getLogger("nbi_auth_sidecar.main")


# ---------------------------------------------------------------------------
# 2. Options dataclass + env loader with type coercion + fallback.
# ---------------------------------------------------------------------------


@dataclass
class Options:
    """Validated options bundle.  ``from_env()`` is the sole constructor."""

    minter_name: str
    http_host: str
    http_port: int
    http_max_threads: int
    refresh_params: RefreshParams
    # Reload client tuning.
    reload_discovery_timeout_sec: int
    reload_discovery_interval_sec: float
    reload_http_timeout_sec: float
    reload_post_attempts: int
    reload_post_backoff_sec: float

    @classmethod
    def from_env(cls) -> "Options":
        def _int(name: str, default: int, *, min_v: Optional[int] = None, max_v: Optional[int] = None) -> int:
            raw = os.environ.get(name)
            if raw is None or raw == "":
                return default
            try:
                val = int(raw)
            except (TypeError, ValueError):
                log.warning("Invalid int %s=%s; using default %d", name, redact_secrets(str(raw)), default)
                return default
            if min_v is not None and val < min_v:
                return min_v
            if max_v is not None and val > max_v:
                return max_v
            return val

        def _float(name: str, default: float, *, min_v: float = 0.0, max_v: float = 1e9) -> float:
            raw = os.environ.get(name)
            if raw is None or raw == "":
                return default
            try:
                val = float(raw)
            except (TypeError, ValueError):
                log.warning("Invalid float %s=%s; using default %.1f", name, redact_secrets(str(raw)), default)
                return default
            return max(min_v, min(max_v, val))

        refresh_params = RefreshParams(
            refresh_before_exp_sec=_int("NBI_REFRESH_BEFORE_EXP_SEC", 300, min_v=30, max_v=3600 * 12),
            force_refresh_interval_sec=_int("NBI_FORCE_REFRESH_INTERVAL_SEC", 0, min_v=0),
            sleep_chunk_sec=_int("NBI_SCHEDULER_SLEEP_CHUNK_SEC", 10, min_v=1, max_v=120),
            bootstrap_timeout_sec=_int("NBI_BOOTSTRAP_TIMEOUT_SEC", 90, min_v=10, max_v=600),
            backoff_base_sec=_int("NBI_BACKOFF_BASE_SEC", 10, min_v=1, max_v=600),
            backoff_cap_sec=_int("NBI_BACKOFF_CAP_SEC", 300, min_v=10, max_v=3600),
        )
        return cls(
            minter_name=(os.environ.get("NBI_SIDECAR_MINTER", "jar") or "jar").lower(),
            http_host=os.environ.get("NBI_SIDECAR_HTTP_HOST", "127.0.0.1") or "127.0.0.1",
            http_port=_int("NBI_SIDECAR_HTTP_PORT", 18090, min_v=1024, max_v=65535),
            http_max_threads=_int("NBI_SIDECAR_HTTP_MAX_THREADS", 8, min_v=2, max_v=64),
            refresh_params=refresh_params,
            reload_discovery_timeout_sec=_int("NBI_RELOAD_DISCOVERY_TIMEOUT_SEC", 60, min_v=5, max_v=300),
            reload_discovery_interval_sec=_float("NBI_RELOAD_DISCOVERY_INTERVAL_SEC", 2.0, min_v=0.5, max_v=30.0),
            reload_http_timeout_sec=_float("NBI_RELOAD_HTTP_TIMEOUT_SEC", 5.0, min_v=1.0, max_v=60.0),
            reload_post_attempts=_int("NBI_RELOAD_POST_ATTEMPTS", 3, min_v=1, max_v=20),
            reload_post_backoff_sec=_float("NBI_RELOAD_POST_BACKOFF_SEC", 2.0, min_v=0.1, max_v=30.0),
        )


# ---------------------------------------------------------------------------
# 3. Signal handling — ensure clean shutdown in < 10s (K8s grace period default).
# ---------------------------------------------------------------------------

#: Set to True by signal handler so the HTTP server shutdown thread knows
#: to avoid double-shutdown on SIGINT followed by SIGTERM.
_SHUTDOWN_REQUESTED = threading.Event()


def _install_signal_handlers(scheduler: TokenScheduler, http_server) -> None:
    """Install handlers for SIGINT (Ctrl-C) and SIGTERM (K8s preStop).

    Python signal semantics are tricky: handlers run on the MAIN thread
    *only when the main thread is about to return to the eval loop*.
    ``serve_forever(poll_interval=0.5)`` periodically runs Python bytecode
    so signal delivery works.  We *do not* call ``http_server.shutdown()``
    inside the signal handler directly — that method blocks until
    ``serve_forever()`` returns, which would deadlock (they'd be on the
    same thread).  Instead we spawn a tiny one-off shutdown daemon
    thread whose sole job is to call ``shutdown()`` and
    ``scheduler.stop()``.
    """

    def _handler(signum: int, _frame) -> None:
        if _SHUTDOWN_REQUESTED.is_set():
            log.warning("Signal %s received TWICE — forcing hard exit in 2s.", signum)
            time.sleep(2.0)
            sys.exit(128 + signum)
        _SHUTDOWN_REQUESTED.set()
        sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        log.info("Signal %s received — starting graceful shutdown (scheduler + HTTP server).", sig_name)
        scheduler.stop()

        def _shutdown_server() -> None:
            try:
                http_server.shutdown()
            except Exception as exc:  # noqa: BLE001
                log.warning("http_server.shutdown() raised (non-fatal): %s", redact_secrets(str(exc)))

        threading.Thread(target=_shutdown_server, daemon=True, name="sidecar-shutdown").start()

    # Only install on platforms with POSIX signals. On Windows the first
    # missing attribute short-circuits and we fall back to plain Ctrl-C via
    # KeyboardInterrupt (serve_forever already handles that case).
    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Raised when run outside main thread (tests). Harmless.
            pass


# ---------------------------------------------------------------------------
# 4. Wiring + main.
# ---------------------------------------------------------------------------


def _validate_required_env() -> list[str]:
    """Return list of human-readable problems for *required* env (empty = ok).

    The STRING_OVERRIDE 8 env vars are needed for config build; we
    validate them up-front so ``sys.exit(2)`` produces a clear error
    instead of a confusing traceback later.  ``ANTHROPIC_API_KEY`` is
    intentionally excluded — the MINTED token is what gets written.
    """
    problems: list[str] = []
    required = {
        "NBI_CHAT_MODEL_PROVIDER": "e.g. openai_compatible",
        "NBI_CHAT_MODEL_ID": "e.g. databricks/gdp-gpt4o",
        "ANTHROPIC_BASE_URL": "AI Factory gateway base URL ending in /v1",
    }
    for name, hint in required.items():
        value = os.environ.get(name)
        if not value:
            problems.append(f"Required env var {name!r} is empty or missing. Hint: {hint}.")
    # Minter-specific checks
    minter_name = (os.environ.get("NBI_SIDECAR_MINTER", "jar") or "jar").lower()
    if minter_name == "jar":
        for env_name in [
            "NBI_TOKEN_TOOL_JAR",
            "NBI_KEYSTORE_JKS",
            "NBI_TRUSTSTORE_JKS",
            "OIDC_TOKEN_URL",
            "OIDC_CLIENT_CODE",
            "OIDC_DOMAIN",
            # Password env vars are also required; naming follows the mint.py
            # SecureJarTokenMinter convention.
            "JKS_KEYSTORE_PASSWORD",
            "JKS_TRUSTSTORE_PASSWORD",
        ]:
            if not os.environ.get(env_name):
                # Non-fatal warning only — the admin may have injected these
                # via a sidecar container command that exports them later.
                log.warning(
                    "JAR minter selected but %s appears empty; check K8s Secret envFrom wiring.",
                    env_name,
                )
    return problems


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point.  Returns POSIX exit code (0 = clean, non-zero = failure)."""
    del argv  # CLI flags are intentionally absent; use env vars only.
    start_wall = time.time()

    # ---- (a) validate env ------------------------------------------------
    problems = _validate_required_env()
    if problems:
        for p in problems:
            log.error("ENV_CONFIG: %s", p)
        log.error(
            "Set required env vars via Helm values.yaml → singleuser.extraEnv (or the pre_spawn_hook.py envFrom block)."
        )
        return 2  # usage / config exit code (convention: 1=runtime, 2=config)

    opts = Options.from_env()
    log.info(
        "NBI Auth Sidecar starting — minter=%s bind=%s:%d refresh_before_exp=%ds force_interval=%ds",
        opts.minter_name,
        opts.http_host,
        opts.http_port,
        opts.refresh_params.refresh_before_exp_sec,
        opts.refresh_params.force_refresh_interval_sec,
    )

    # ---- (b) construct collaborators in dependency order -----------------
    try:
        minter = build_token_minter(env_minter_name=opts.minter_name)
    except Exception as exc:  # noqa: BLE001
        log.error("Could not construct %s minter: %s", opts.minter_name, redact_secrets(str(exc)))
        return 2

    reload_client = DefaultNBIReloadClient(
        discovery_timeout_sec=opts.reload_discovery_timeout_sec,
        discovery_interval_sec=opts.reload_discovery_interval_sec,
        http_timeout_sec=opts.reload_http_timeout_sec,
        post_attempts=opts.reload_post_attempts,
        post_backoff_sec=opts.reload_post_backoff_sec,
    )

    try:
        scheduler = build_scheduler_from_env(
            minter=minter,
            reload_client=reload_client,
            params=opts.refresh_params,
        )
    except RuntimeError as exc:
        log.error("build_scheduler_from_env failed (missing env?): %s", redact_secrets(str(exc)))
        return 2

    http_server = build_server(
        scheduler,
        host=opts.http_host,
        port=opts.http_port,
        max_threads=opts.http_max_threads,
    )
    # Bind signal handlers LAST — avoids delivering SIGTERM while we're
    # still 1/2 constructed and scheduler/http_server references None.
    _install_signal_handlers(scheduler, http_server)

    # ---- (c) synchronous bootstrap.  Fail hard on permanent failure. -----
    log.info(
        "Starting synchronous bootstrap (timeout=%ds) — will exit(1) if no token after deadline.",
        opts.refresh_params.bootstrap_timeout_sec,
    )
    bootstrap_ok = scheduler.bootstrap()
    if not bootstrap_ok:
        log.error(
            "Bootstrap FAILED permanently after %.1fs — exiting process with code 1 so K8s restarts sidecar container.",
            time.time() - start_wall,
        )
        # Best-effort stop before os._exit.
        try:
            scheduler.stop()
        except Exception:
            pass
        return 1
    log.info("Bootstrap OK — took %.1fs total.", time.time() - start_wall)

    # ---- (d) start scheduler daemon thread. -------------------------------
    # The daemon flag matters: when the main thread's serve_forever()
    # exits (on signal / shutdown) Python can exit without waiting for
    # the scheduler's 10s sleep chunk to finish.
    scheduler_thread = threading.Thread(
        target=scheduler.run_forever,
        name="nbi-auth-scheduler",
        daemon=True,
    )
    scheduler_thread.start()
    log.info("Scheduler daemon thread spawned (name=nbi-auth-scheduler pid=%d tid native).", os.getpid())

    # ---- (e) serve HTTP on MAIN thread (signal compatibility). -----------
    try:
        http_server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt in main().")
    finally:
        try:
            http_server.server_close()
        except Exception as exc:  # noqa: BLE001
            log.warning("server_close raised: %s", redact_secrets(str(exc)))
    # Join scheduler briefly so its final log line arrives before we flush.
    scheduler.stop()
    scheduler_thread.join(timeout=3.0)
    total_life = time.time() - start_wall
    log.info("Sidecar exit. Total uptime %.1fs.", total_life)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
