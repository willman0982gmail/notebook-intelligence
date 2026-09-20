"""
scheduler.py — Token lifecycle scheduler.

Responsibilities
----------------
1. Wrap a single ``TokenMinter`` + ``write_all_from_token`` writer function
   + ``NBIReloadClient`` in a run-loop that refreshes the AI Factory
   access token EXACTLY ``NBI_REFRESH_BEFORE_EXP_SEC`` (default 300 = 5m)
   before the JWT's ``exp`` claim.
2. Enforce a second, wall-clock upper bound via
   ``NBI_FORCE_REFRESH_INTERVAL_SEC`` (default 0 = disabled).  Set to a
   positive value (e.g. 3600) when IdPs silently revoke long-lived tokens
   whose ``exp`` claim has not yet been reached.
3. Sleep in 10-second chunks so ``SIGTERM``-style shutdown and manual
   ``/rotate-self`` HTTP requests never block for longer than a tick.
4. Persist scheduler state to disk on every completed cycle so a
   restarting sidecar can pick up the *last-known-exp* instead of
   blindly re-minting on every pod bounce.
5. Expose ``trigger_refresh_now()`` so the HTTP server can request an
   immediate out-of-band cycle (admin-initiated rotation, or debug).

Thread safety
-------------
``TokenScheduler`` is designed to be driven from two threads:
  * The *run-loop thread* (daemon) calls ``run_forever()`` and spends
    99.9% of its time in ``_sleep_interruptible()``.
  * The *HTTP server thread* (pooled handler) occasionally calls
    ``trigger_refresh_now()``.

All shared state mutations happen under ``_state_lock``.  The mint
operation itself is serialised by the outer ``RetryingTokenMinter``
lock, so concurrent trigger requests never issue parallel JAR calls.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping, MutableMapping, Optional, Protocol, Tuple

from .config_writer import (
    atomic_write_json,
    default_runtime_env_file,
    default_user_config_file,
    write_all_from_token,
)
from .mint import AccessToken, TokenMinter, _backoff_seconds  # sibling import, no external deps

log = logging.getLogger("nbi_auth_sidecar.scheduler")

# ---------------------------------------------------------------------------
# 1. Configuration defaults — all overridable via env so helm values.yaml
#    has something declarative to set per-install.
# ---------------------------------------------------------------------------

#: How many seconds before ``exp`` to proactively refresh.
DEFAULT_REFRESH_BEFORE_EXP_SEC = int(
    os.environ.get("NBI_REFRESH_BEFORE_EXP_SEC", "300")
)
#: Maximum time between refreshes, regardless of JWT TTL.  ``0`` = disabled.
DEFAULT_FORCE_REFRESH_INTERVAL_SEC = int(
    os.environ.get("NBI_FORCE_REFRESH_INTERVAL_SEC", "0")
)
#: Granularity of the sleep ticks.  Smaller values make /rotate-self snappier
#: at the cost of spurious wakeups.  10s is a nice sweet spot.
DEFAULT_SLEEP_CHUNK_SEC = int(
    os.environ.get("NBI_SCHEDULER_SLEEP_CHUNK_SEC", "10")
)
#: Maximum total time allowed for the initial bootstrap cycle.  If we can't
#: get a token onto disk in this window, we consider the sidecar unhealthy
#: and let K8s crash-loop it.
DEFAULT_BOOTSTRAP_TIMEOUT_SEC = int(
    os.environ.get("NBI_BOOTSTRAP_TIMEOUT_SEC", "90")
)
#: Backoff parameters used for transient mint failures during *steady
#: state* (bootstrap has its own tighter loop inside RetryingTokenMinter).
DEFAULT_BACKOFF_BASE_SEC = int(
    os.environ.get("NBI_BACKOFF_BASE_SEC", "10")
)
DEFAULT_BACKOFF_CAP_SEC = int(
    os.environ.get("NBI_BACKOFF_CAP_SEC", "300")
)
#: Scheduler state path.  Lives on shared empty-dir ``/tmp`` so in-memory
#: state survives a container restart within the same pod.
DEFAULT_STATE_FILE = os.environ.get(
    "NBI_TOKEN_STATE_FILE", "/tmp/nbi-token-state.json"
)

#: State file version bump flag.  Increment this whenever fields change in
#: a non-backward-compatible way so the scheduler starts fresh.
STATE_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# 2. Protocol for the reload client — scheduler only cares about the
#    return tuple, not the implementation.  Concrete impl lives in
#    nbi_reload_client.py (task 5).
# ---------------------------------------------------------------------------


class NBIReloadClient(Protocol):
    """Thin duck-type interface for the NBI reload-config trigger.

    Implementations must never raise for transient HTTP errors; instead
    they MUST return a ``(success: bool, detail: dict)`` tuple so the
    scheduler can degrade gracefully (S2 FS-poller will eventually
    refresh anyway).
    """

    def reload(self, *, broadcast: bool = True) -> tuple[bool, dict]:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# 3. SchedulerState dataclass — persisted to disk, NEVER contains raw token.
# ---------------------------------------------------------------------------


@dataclass
class SchedulerState:
    """Snapshot of scheduler progress — persisted on every completed cycle.

    The whole object is intentionally small and metadata-only.  The raw
    bearer token is intentionally never written to this file so a
    low-privilege shell that can read ``/tmp`` still cannot escalate to
    API access.
    """

    #: ``time.time()``-style timestamp of the last successful mint.
    last_refresh_ts: float = 0.0
    #: Human-readable reason for last refresh ("bootstrap", "expiry",
    #: "force_interval", "manual", "startup_cached").
    last_refresh_reason: str = ""
    #: ``time.time()``-style timestamp of *current known* exp, ``0.0`` if
    #: no token has ever been minted.
    token_expires_at: float = 0.0
    #: Length of the access token string (sanity check only — no secrets).
    token_len: int = 0
    #: Number of *consecutive* mint failures since the last success.
    #: Used to drive exponential backoff and readiness probe.
    consecutive_mint_failures: int = 0
    #: Incrementing counter of successful refreshes, useful for /metrics.
    successful_refresh_count: int = 0
    #: Incrementing counter of *attempted* refreshes (success + fail).
    attempted_refresh_count: int = 0
    #: Schema version — STATE_SCHEMA_VERSION in the running binary.  If
    #: the on-disk value differs we discard state and start clean.
    version: int = STATE_SCHEMA_VERSION

    @property
    def has_valid_token(self) -> bool:
        """True iff a token has been minted AND we think it's still live."""
        if self.token_expires_at <= 0 or self.token_len <= 0:
            return False
        # Require at least 10s of remaining TTL before reporting "valid".
        return self.token_expires_at > (time.time() + 10.0)


def _load_state(path: str) -> SchedulerState:
    """Load and validate state from ``path``, returning a clean state on any problem."""
    if not os.path.exists(path):
        return SchedulerState()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = dict(__import__("json").load(fh))
    except (OSError, ValueError) as exc:
        log.warning("Discarding unparseable state file %s: %s", path, exc)
        return SchedulerState()
    if raw.get("version") != STATE_SCHEMA_VERSION:
        log.info(
            "State schema version %s != running %s — starting fresh.",
            raw.get("version"),
            STATE_SCHEMA_VERSION,
        )
        return SchedulerState()
    try:
        return SchedulerState(**{k: raw[k] for k in raw.keys() if k in SchedulerState.__dataclass_fields__})
    except TypeError as exc:
        log.warning("State schema field mismatch: %s", exc)
        return SchedulerState()


def _save_state(path: str, state: SchedulerState) -> None:
    """Persist state atomically.  Uses 0600 so shell users can't modify counters."""
    atomic_write_json(path, asdict(state), mode=0o600)


# ---------------------------------------------------------------------------
# 4. Refresh parameters dataclass — groups helm/admin-knobs together.
# ---------------------------------------------------------------------------


@dataclass
class RefreshParams:
    """All admin-tunable scheduler knobs in one bag."""

    refresh_before_exp_sec: int = DEFAULT_REFRESH_BEFORE_EXP_SEC
    force_refresh_interval_sec: int = DEFAULT_FORCE_REFRESH_INTERVAL_SEC
    sleep_chunk_sec: int = DEFAULT_SLEEP_CHUNK_SEC
    bootstrap_timeout_sec: int = DEFAULT_BOOTSTRAP_TIMEOUT_SEC
    backoff_base_sec: int = DEFAULT_BACKOFF_BASE_SEC
    backoff_cap_sec: int = DEFAULT_BACKOFF_CAP_SEC

    @classmethod
    def from_env(cls) -> "RefreshParams":
        return cls(
            refresh_before_exp_sec=DEFAULT_REFRESH_BEFORE_EXP_SEC,
            force_refresh_interval_sec=DEFAULT_FORCE_REFRESH_INTERVAL_SEC,
            sleep_chunk_sec=DEFAULT_SLEEP_CHUNK_SEC,
            bootstrap_timeout_sec=DEFAULT_BOOTSTRAP_TIMEOUT_SEC,
            backoff_base_sec=DEFAULT_BACKOFF_BASE_SEC,
            backoff_cap_sec=DEFAULT_BACKOFF_CAP_SEC,
        )


# ---------------------------------------------------------------------------
# 5. Core TokenScheduler
# ---------------------------------------------------------------------------


class TokenScheduler:
    """Glue object: minter → writer → reload → state → sleep loop."""

    def __init__(
        self,
        minter: TokenMinter,
        reload_client: NBIReloadClient,
        *,
        # Provider & model IDs — come from helm values.yaml → env or __main__ CLI
        provider: str,
        chat_model_id: str,
        inline_model_id: str,
        base_url: str,
        claude_chat_model: Optional[str] = None,
        claude_inline_model: Optional[str] = None,
        # Output paths (useful for tests; defaults match config_writer)
        config_path: Optional[str] = None,
        runtime_env_path: Optional[str] = None,
        merge_existing_config: bool = True,
        state_path: Optional[str] = None,
        params: Optional[RefreshParams] = None,
        # Hookable for testing
        _time_fn: Callable[[], float] = time.time,
        _writer_fn: Callable[..., Tuple[str, str]] = write_all_from_token,
    ) -> None:
        self._minter = minter
        self._reload_client = reload_client
        self._provider = provider
        self._chat_model_id = chat_model_id
        self._inline_model_id = inline_model_id
        self._base_url = base_url
        self._claude_chat_model = claude_chat_model
        self._claude_inline_model = claude_inline_model
        self._config_path = config_path or default_user_config_file()
        self._runtime_env_path = runtime_env_path or default_runtime_env_file()
        self._merge_existing_config = merge_existing_config
        self._state_path = state_path or DEFAULT_STATE_FILE
        self._params = params or RefreshParams.from_env()
        self._time_fn = _time_fn
        self._writer_fn = _writer_fn

        # Thread safety primitives.
        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._trigger_event = threading.Event()

        # Load (or initialise) the persisted state object.
        self._state = _load_state(self._state_path)
        # Hint about what we loaded so an operator reading boot logs can
        # tell whether this is a cold start or a sidecar restart within
        # the same pod lifetime.
        if self._state.has_valid_token:
            log.info(
                "Loaded state: last refresh %ds ago, exp in %ds, reason=%s",
                int(self._time_fn() - self._state.last_refresh_ts),
                int(self._state.token_expires_at - self._time_fn()),
                self._state.last_refresh_reason,
            )
        else:
            log.info("No usable persisted state — will do bootstrap mint.")

    # ------------------------------------------------------------------
    # Public observation helpers — used by server.py for /healthz,
    # /ready, /metrics endpoints.  All read state under the lock.
    # ------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        """True iff the last known token is valid AND mint isn't failing."""
        with self._state_lock:
            if not self._state.has_valid_token:
                return False
            # Allow up to 3 transient failures before we start reporting
            # NotReady.  RetryingTokenMinter already tried 5 times per
            # cycle, so "3 consecutive failures" ≈ 15 real attempts →
            # definitely a systemic problem K8s should page on.
            return self._state.consecutive_mint_failures < 3

    def snapshot_state(self) -> SchedulerState:
        """Return a COPY of the internal state — safe to pass across threads."""
        with self._state_lock:
            return SchedulerState(**asdict(self._state))

    # ------------------------------------------------------------------
    # Manual trigger — called from HTTP /rotate-self handler.
    # ------------------------------------------------------------------

    def trigger_refresh_now(self) -> None:
        """Request an out-of-band refresh cycle.

        The run-loop thread will start executing ``do_refresh_cycle``
        within ``sleep_chunk_sec`` of this call.  Multiple invocations
        before the cycle starts coalesce into a single refresh (that's
        the whole point of an event vs a counter).
        """
        log.info("Manual refresh requested via trigger_refresh_now()")
        self._trigger_event.set()

    # ------------------------------------------------------------------
    # Lifecycle — called from __main__.  ``stop()`` is idempotent.
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Signal the run-loop to exit at the next 10s tick boundary."""
        log.info("Scheduler stop() called — run-loop will exit after current tick.")
        self._stop_event.set()
        # Also unblock a wait_for_trigger() so shutdown is < chunk_sec even
        # when we were comfortably napping until T-300s.
        self._trigger_event.set()

    # ------------------------------------------------------------------
    # Core business logic.
    # ------------------------------------------------------------------

    def compute_next_run(self) -> float:
        """Return absolute ``time.time()``-style timestamp of the next scheduled run.

        The next run is the EARLIEST of:
          (a) ``token_expires_at - refresh_before_exp_sec`` — i.e. the
              "5 minutes before exp" rule.
          (b) ``last_refresh_ts + force_refresh_interval_sec`` — when the
              IdP silently revokes tokens whose ``exp`` hasn't fired yet.
              Skipped when force_refresh_interval_sec is ``0``.

        If no valid token exists return ``now`` — i.e. "run immediately".
        """
        now = self._time_fn()
        state = self.snapshot_state()

        if not state.has_valid_token:
            return now

        candidates = [state.token_expires_at - self._params.refresh_before_exp_sec]
        if self._params.force_refresh_interval_sec > 0:
            candidates.append(state.last_refresh_ts + self._params.force_refresh_interval_sec)
        return min(candidates)

    def _sleep_interruptible(self, until_ts: float) -> bool:
        """Sleep in ``sleep_chunk_sec`` bites until ``until_ts`` or stop/trigger.

        Returns ``True`` when woken by a *manual trigger* (caller should
        run the cycle immediately).  Returns ``False`` when the scheduled
        time was reached OR stop was signalled (caller should re-check
        stop_event before looping).
        """
        chunk = max(1, self._params.sleep_chunk_sec)
        while True:
            if self._stop_event.is_set():
                return False
            # Manual-trigger short-circuits the nap regardless of deadline.
            if self._trigger_event.is_set():
                # Clear the trigger flag so the NEXT trigger is an event.
                self._trigger_event.clear()
                return True
            now = self._time_fn()
            remaining = until_ts - now
            if remaining <= 0:
                return False
            to_sleep = min(chunk, remaining)
            # Event.wait is the primitive that lets stop_event wake us up
            # mid-nap.  We *also* sleep on trigger_event so /rotate-self
            # can fire within a tick.
            if self._trigger_event.wait(timeout=to_sleep):
                self._trigger_event.clear()
                return True

    def do_refresh_cycle(self, reason: str) -> bool:
        """Execute ONE full refresh cycle end-to-end.

        Ordering guarantees (non-negotiable because the 5-layer cache
        chain depends on every step):
            1. ``minter.mint()`` → fresh AccessToken.
            2. Write both config files atomically (L1 disk layer).
            3. POST /notebook-intelligence/reload-config with broadcast=1
               so NBI backend does L2→L3 reload AND pushes WS msg
               MCPServerStatusChange → L4/L5 refresh.
            4. Persist state → next boot doesn't re-mint unnecessarily.
            5. Update Prometheus counters.

        Returns ``True`` on success.  Failure is *never* raised — it's
        recorded in state.consecutive_mint_failures so readiness probes
        and exponential backoff can react accordingly.
        """
        p = self._params
        cycle_started = self._time_fn()
        log.info(
            "▶ Refresh cycle START — reason=%s (prev_failures=%d, token_ttl_left=%ds)",
            reason,
            self._state.consecutive_mint_failures,
            max(0, int(self._state.token_expires_at - cycle_started)),
        )

        # ---- Step 1: mint --------------------------------------------------
        token: Optional[AccessToken] = None
        try:
            token = self._minter.mint()
            if token is None or not getattr(token, "access_token", None):
                raise RuntimeError("minter.mint() returned empty token")
        except Exception as exc:  # noqa: BLE001 — we intentionally eat everything
            with self._state_lock:
                self._state.attempted_refresh_count += 1
                self._state.consecutive_mint_failures += 1
                _save_state(self._state_path, self._state)
            failure_idx = self._state.consecutive_mint_failures
            backoff = _backoff_seconds(
                failure_idx,
                base=p.backoff_base_sec,
                cap=p.backoff_cap_sec,
            )
            log.warning(
                "✖ Mint FAILED (consecutive=%d, backoff=%ds, exc=%s)",
                failure_idx,
                backoff,
                exc,
            )
            # Sleep the backoff BEFORE returning so callers in a tight loop
            # (e.g. bootstrap for/else) don't hammer the IdP.
            self._sleep_interruptible(self._time_fn() + backoff)
            return False

        # ---- Step 2: write config files ------------------------------------
        try:
            self._writer_fn(
                access_token=token.access_token,
                provider=self._provider,
                chat_model_id=self._chat_model_id,
                inline_model_id=self._inline_model_id,
                base_url=self._base_url,
                claude_chat_model=self._claude_chat_model,
                claude_inline_model=self._claude_inline_model,
                config_path=self._config_path,
                runtime_env_path=self._runtime_env_path,
                merge_existing_config=self._merge_existing_config,
            )
        except Exception as exc:  # noqa: BLE001
            with self._state_lock:
                self._state.attempted_refresh_count += 1
                self._state.consecutive_mint_failures += 1
                _save_state(self._state_path, self._state)
            log.error("✖ Config write FAILED: %s", exc)
            # Even on write failure we treat this as a refresh failure.
            return False

        # ---- Step 3: POST reload-config ------------------------------------
        reload_ok = False
        reload_detail: Mapping[str, Any] = {}
        try:
            reload_ok, reload_detail = self._reload_client.reload(broadcast=True)
        except Exception as exc:  # noqa: BLE001 — duck-type guard.
            log.warning("Reload client raised unexpectedly (will treat as non-fatal): %s", exc)
        if not reload_ok:
            # Non-fatal: S2 FS-poller running inside the notebook container
            # will eventually pick up the files.  Just warn loudly.
            log.warning(
                "Reload-config POST did not succeed — FS poller fallback will apply.  detail=%s",
                reload_detail,
            )

        # ---- Step 4 + 5: update state + counters ---------------------------
        with self._state_lock:
            self._state.last_refresh_ts = cycle_started
            self._state.last_refresh_reason = reason
            self._state.token_expires_at = float(token.expires_at)
            self._state.token_len = len(token.access_token)
            self._state.consecutive_mint_failures = 0
            self._state.successful_refresh_count += 1
            self._state.attempted_refresh_count += 1
            _save_state(self._state_path, self._state)

        cycle_took = self._time_fn() - cycle_started
        ttl_left = token.expires_at - self._time_fn()
        log.info(
            "✔ Refresh cycle OK — reason=%s ttl_left=%ds wrote_cfg=%s took=%.1fs reload_ok=%s",
            reason,
            int(ttl_left),
            os.path.basename(self._config_path),
            cycle_took,
            reload_ok,
        )
        return True

    # ------------------------------------------------------------------
    # Synchronous bootstrap block — called BEFORE run_forever() from the
    # main thread.  On permanent failure we sys.exit(1) so K8s marks the
    # sidecar container as crash-looping.
    # ------------------------------------------------------------------

    def bootstrap(self) -> bool:
        """Attempt the initial refresh cycle with a hard ``bootstrap_timeout_sec`` cap.

        Returns ``True`` on success, ``False`` on timeout / permanent failure.
        Caller is responsible for ``sys.exit(1)`` when False.
        """
        deadline = self._time_fn() + self._params.bootstrap_timeout_sec
        log.info(
            "Bootstrap phase started — deadline in %ds, prior state.valid_token=%s",
            self._params.bootstrap_timeout_sec,
            self.snapshot_state().has_valid_token,
        )
        # If we already have a usable persisted state AND we can reach the
        # reload endpoint quickly, accept the cached token instead of
        # issuing a redundant mint.  This matters because IdPs will
        # happily rate-limit you if every pod restart re-mints.
        if self.snapshot_state().has_valid_token:
            log.info("Re-using persisted token for bootstrap.")
            cached_ok = self.do_refresh_cycle(reason="startup_cached")
            if cached_ok:
                return True
            # Fall through — persisted state may have been from yesterday's
            # pod and the token was revoked server-side despite exp.
        # Attempt loop.  RetryingTokenMinter does per-mint retries; here
        # we wrap the *whole* cycle so transient write failures also get
        # a couple of chances.
        attempt = 0
        while self._time_fn() < deadline and not self._stop_event.is_set():
            attempt += 1
            log.info("Bootstrap attempt #%d …", attempt)
            if self.do_refresh_cycle(reason="bootstrap"):
                return True
            # do_refresh_cycle already slept the per-failure backoff — no
            # extra sleep here.
        log.error(
            "Bootstrap FAILED after %d attempt(s) over %.1fs.  Reporting unhealthy.",
            attempt,
            self._time_fn() - (deadline - self._params.bootstrap_timeout_sec),
        )
        return False

    # ------------------------------------------------------------------
    # Run-loop — called as a daemon target (or inline in tests).
    # ------------------------------------------------------------------

    def run_forever(self) -> None:
        """Block forever running scheduled refresh cycles.

        Exits cleanly on the first ``stop()`` or when the process is torn
        down.  Intended to be launched as a daemon thread so
        ``ThreadingHTTPServer.serve_forever`` can own the main thread.
        """
        log.info(
            "Scheduler run-loop START — refresh_before=%ds force_interval=%ds sleep_chunk=%ds",
            self._params.refresh_before_exp_sec,
            self._params.force_refresh_interval_sec,
            self._params.sleep_chunk_sec,
        )
        while not self._stop_event.is_set():
            next_run = self.compute_next_run()
            now = self._time_fn()
            reason = "scheduled"
            if next_run <= now:
                # Immediately due — could be a never-minted state, could
                # be an exp-based wakeup in the past, or a manual
                # trigger coalesced with the normal sleep.
                if self.snapshot_state().last_refresh_ts == 0:
                    reason = "scheduled:cold"
                else:
                    # Classify why compute_next_run returned <= now so
                    # logs make it obvious.
                    state = self.snapshot_state()
                    exp_deadline = state.token_expires_at - self._params.refresh_before_exp_sec
                    force_deadline = (
                        state.last_refresh_ts + self._params.force_refresh_interval_sec
                        if self._params.force_refresh_interval_sec > 0
                        else float("inf")
                    )
                    if exp_deadline <= now:
                        reason = "scheduled:expiry"
                    elif force_deadline <= now:
                        reason = "scheduled:force_interval"
                    else:
                        reason = "scheduled:expired_sleep"
                self.do_refresh_cycle(reason=reason)
                continue

            # Sleep until next_run or trigger.
            secs_until = int(next_run - now)
            log.info(
                "💤 Scheduler napping %ds until %s (next_run in %ds, last_reason=%s)",
                secs_until,
                time.strftime("%H:%M:%S", time.localtime(next_run)),
                secs_until,
                self.snapshot_state().last_refresh_reason,
            )
            manual_wake = self._sleep_interruptible(next_run)
            if manual_wake:
                # /rotate-self handler pinged us.
                self.do_refresh_cycle(reason="manual")
        log.info("Scheduler run-loop EXIT — stop_event is set.")


# ---------------------------------------------------------------------------
# 6. Convenience: build a default scheduler from env + injected deps.
# ---------------------------------------------------------------------------


def build_scheduler_from_env(
    minter: TokenMinter,
    reload_client: NBIReloadClient,
    *,
    provider_env: str = "NBI_CHAT_MODEL_PROVIDER",
    chat_env: str = "NBI_CHAT_MODEL_ID",
    inline_env: str = "NBI_INLINE_COMPLETION_MODEL_ID",
    base_url_env: str = "ANTHROPIC_BASE_URL",
    claude_chat_env: str = "NBI_CLAUDE_CHAT_MODEL",
    claude_inline_env: str = "NBI_CLAUDE_INLINE_COMPLETION_MODEL",
    params: Optional[RefreshParams] = None,
    **kwargs: Any,
) -> TokenScheduler:
    """Create a TokenScheduler reading provider/model IDs from the 8 STRING_OVERRIDE envs.

    Extra ``kwargs`` override any TokenScheduler constructor arg
    (``config_path``, ``state_path``, etc. — used by tests).
    """

    def _require(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise RuntimeError(
                f"Required env var {name!r} is empty.  "
                "Populate it via helm values.yaml → singleuser.extraEnv or pre_spawn_hook.py."
            )
        return value

    return TokenScheduler(
        minter=minter,
        reload_client=reload_client,
        provider=_require(provider_env),
        chat_model_id=_require(chat_env),
        inline_model_id=os.environ.get(inline_env) or _require(chat_env),
        base_url=_require(base_url_env),
        claude_chat_model=os.environ.get(claude_chat_env) or None,
        claude_inline_model=os.environ.get(claude_inline_env) or None,
        params=params,
        **kwargs,
    )
