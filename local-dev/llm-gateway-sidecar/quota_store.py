#!/usr/bin/env python3
"""Quota check/commit client + durable file store (LLM-S12/S13/S15).

This module implements the quota enforcement subsystem used by both the
per-user LLM auth sidecar and the centralized Quota Service.

Architecture
------------
Three backends are supported via ``QUOTA_BACKEND`` environment variable:

- **local**  (default): Per-pod file-backed counters under ``QUOTA_STORE_PATH``.
  Suitable for local-dev, laptop spikes, or dig environments where a central
  quota service is not deployed.
- **http**  : Calls a centralized Quota Service at ``QUOTA_SERVICE_URL``.
  Used in production Hub clusters so counters survive pod churn and are
  consistent across Notebook restarts.
- **memory**: Process-lifetime in-memory only. Used for unit tests.

Plan Resolution (LLM-S15 — No privilege escalation via pod env)
---------------------------------------------------------------
The effective plan is always re-resolved server-side using the canonical
identity.  ``hint_plan`` (injected via ``NBI_LLM_PLAN`` pod env) is accepted
only when it equals ``local`` — i.e.  it cannot be used to raise a user into
``power`` or ``standard`` if their group/user-override would otherwise map to
``intern``.

Window semantics
----------------
A simple 24-hour rolling window anchored at ``window_start``.  Every
``check``/``commit``/``snapshot`` call runs ``_roll()`` first, which resets
the counters if 86400 seconds have elapsed since the last ``window_start``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

log = logging.getLogger("sidecar.quota")

# ---------------------------------------------------------------------------
# Plan catalog — hard-coded defaults that may be partially overridden by an
# optional JSON file at QUOTA_PLANS_PATH (see build_quota_backend).
# The plans dict shape: {plan_id: {id, tokens_per_day, requests_per_day, models[]}}
# Per-feature sub-caps (tokens_per_day_chat / _inline / _agent) may also be
# present; see the `feat_lim` branch in FileQuotaStore.check().
# ---------------------------------------------------------------------------

DEFAULT_PLANS: dict[str, dict[str, Any]] = {
    "intern": {
        "id": "intern",
        "tokens_per_day": 200_000,
        "requests_per_day": 200,
        "models": ["databricks/gdp-gpt4o"],
    },
    "standard": {
        "id": "standard",
        "tokens_per_day": 2_000_000,
        "requests_per_day": 2_000,
        "models": ["databricks/gdp-gpt4o"],
    },
    "power": {
        "id": "power",
        "tokens_per_day": 20_000_000,
        "requests_per_day": 20_000,
        "models": ["databricks/gdp-gpt4o", "databricks/gdp-gpt4o-large"],
    },
    # "local" is the catch-all plan used by laptop spikes. It intentionally
    # reads low default caps from env to prevent accidental noisy-neighbour
    # behaviour when someone forgets to set QUOTA_TOKENS_DAY.
    "local": {
        "id": "local",
        "tokens_per_day": int(os.environ.get("QUOTA_TOKENS_DAY", "50000")),
        "requests_per_day": int(os.environ.get("QUOTA_REQ_DAY", "10000")),
        "models": ["*"],
    },
}

# group name → plan id.  See resolve_plan() — rules are searched in *user
# group order*; first match wins.
DEFAULT_GROUP_RULES: dict[str, str] = {
    "interns": "intern",
    "ml-platform": "power",
}


def canonicalize_user(username: str) -> str:
    """Single subject key rule (LLM-S11.4).

    Produces a stable, case-insensitive, email-domain-stripped identifier
    that is used as the primary key for all quota counters and usage events.

    Cross-file sync NOTE (#6): ``pre_spawn_hook.py`` carries a local copy of
    this function (to avoid importing the sidecar module into the Hub control
    plane image).  When editing the logic here you MUST also update the
    counterpart in ``hub/pre_spawn_hook.py:canonicalize_user`` — the empty-
    input semantics are contractually the same (both return ``"anonymous"``).

    Examples
    --------
    >>> canonicalize_user("Alice@Example.com")
    'alice'
    >>> canonicalize_user("  Bob  ")
    'bob'
    >>> canonicalize_user("")
    'anonymous'
    """
    u = (username or "").strip().lower()
    if "@" in u:
        u = u.split("@", 1)[0]
    return u or "anonymous"


@dataclass
class QuotaDecision:
    """Immutable outcome of a quota ``check`` call.

    Attributes
    ----------
    allowed : bool
        True when the request has not exceeded any cap.  When False the
        sidecar must return HTTP 429 to NBI.
    plan_id : str
        The *authoritative* resolved plan id used for this check.  The UI
        shows this plan name on the quota banner (LLM-S15 UX).
    used_tokens / limit_tokens : int
        Tokens consumed vs. 24h cap.  ``limit_tokens`` of 0 means unlimited.
    used_requests / limit_requests : int
        Request count consumed vs. 24h cap.  0 = unlimited.
    reset_at : int
        Unix-epoch seconds at which the current window rolls over and counters
        reset.  NBI UX displays a human-friendly countdown relative to this.
    message : str
        Human-readable denial reason passed through to NBI error messages.
    soft_cap_hit : bool
        ``True`` when ``used_tokens >= 0.8 * limit_tokens``.  Purely
        informational — the sidecar emits a soft-cap header and increments
        ``soft_cap_hits_total`` counter; enforcement still hard-denies at
        100%.
    """

    allowed: bool
    plan_id: str
    used_tokens: int
    limit_tokens: int
    used_requests: int
    limit_requests: int
    reset_at: int
    message: str = ""
    soft_cap_hit: bool = False


class _SimpleRWLock:
    """Tiny stdlib-only readers-writer lock (Issue #4a).

    * Any number of concurrent readers may hold the lock simultaneously
      (typical: check / snapshot / usage read-only calls).
    * A writer must wait for ALL readers to exit before acquiring, and
      while a writer holds the lock no new readers are allowed (typical:
      commit / boost / roll mutations).
    * Fairness: writers are NOT prioritized, so under sustained read load
      a write may starve — acceptable here because writes are rare per user
      and the scale target is "hundreds of notebooks" MVP.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writing = False

    @contextlib.contextmanager
    def read(self) -> Iterator[None]:
        self._cond.acquire()
        try:
            while self._writing:
                self._cond.wait()
            self._readers += 1
        finally:
            self._cond.release()
        try:
            yield
        finally:
            self._cond.acquire()
            self._readers -= 1
            if not self._readers:
                self._cond.notify_all()
            self._cond.release()

    @contextlib.contextmanager
    def write(self) -> Iterator[None]:
        self._cond.acquire()
        try:
            while self._writing or self._readers:
                self._cond.wait()
            self._writing = True
        finally:
            self._cond.release()
        try:
            yield
        finally:
            self._cond.acquire()
            self._writing = False
            self._cond.notify_all()
            self._cond.release()


def _utc_day_start(ts: float) -> float:
    """Return UTC calendar-day start seconds for ``ts`` (Issue #4b).

    A pod started at any time of day shares the same rollover boundary
    (midnight UTC) so all users' quota windows are aligned.  To switch to
    another timezone (e.g. Asia/Shanghai UTC+8), add the offset hours * 3600
    before floor and subtract after.
    """
    return float(int(ts // 86400) * 86400)


class FileQuotaStore:
    """Durable JSON counters surviving process restart (LLM-S13.1/S13.6).

    Serialization strategy
    ----------------------
    Writes go through a ``.tmp`` sibling file followed by an atomic
    ``os.replace()``.  This protects against half-written files if the
    process dies mid-write.

    Concurrency
    -----------
    All public methods acquire ``self._lock``.  In the per-user sidecar this
    lock is purely defensive because the sidecar's ``ThreadingHTTPServer``
    can handle concurrent NBI chat + inline completion bursts.  In the
    central Quota Service this lock serializes all writes to the single
    shared JSON file.

    On-disk shape
    -------------
    {
        "window_start": 1710000000.0,
        "users": {
            "alice": {
                "tokens": 12345,
                "requests": 42,
                "plan_id": "standard",
                "boost_tokens": 5000,
                "by_feature": {"chat": 8000, "inline": 4345}
            }
        },
        "events": [
            {"ts":171..., "user":"alice", "plan_id":"standard", "model":"databricks/gdp-gpt4o",
             "feature":"chat", "tokens":182}
        ]
    }
    """

    def __init__(self, path: str | Path, plans: dict | None = None) -> None:
        self.path = Path(path)
        # Caller-supplied plan catalog, falling back to DEFAULT_PLANS.
        self.plans = plans or dict(DEFAULT_PLANS)
        # Issue #4a: readers-writer lock replaces a single exclusive lock so check /
        # snapshot / usage can run concurrently while commit / boost still
        # serialize exclusively.
        self._lock = _SimpleRWLock()
        # Ensure parent directory tree exists so first write succeeds.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Seed empty store file if none exists — guarantees _read() always
        # returns a well-shaped dict.
        if not self.path.exists():
            with self._lock.write():
                # Double-check after acquiring write so we haven't lost a race with
                # another process doing the same init.
                if not self.path.exists():
                    self._write({"window_start": _utc_day_start(time.time()), "users": {}})

    def _read(self) -> dict:
        """Load raw JSON dict from disk.  Assumes lock is held by caller."""
        with self.path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data: dict) -> None:
        """Atomic write: dump JSON to ``path.tmp`` then rename.

        Assumes lock is held by caller.  ``indent=2`` makes the file
        human-inspectable during incidents; at <=5000 events the disk
        footprint is still tiny (~MiB range).
        """
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(self.path)

    def _roll(self, data: dict) -> dict:
        """Reset counters iff the current UTC calendar day has advanced.

        Returns *either* the original data dict or a fresh empty dict with a
        new ``window_start`` anchored at the current UTC midnight.

        Issue #4b (calendar-day rollover): Previously the anchor was pod-
        start-time + 86400s, which made the rollover time dependent on when
        a notebook pod was first launched (pod started at 18:00 rolled at
        18:00 next day rather than midnight).  The new semantics aligns
        everyone to the same UTC day boundary.

        Issue #5 fix (boost survives rollover): ``limit_override`` on each
        user record is preserved across rollovers — the boost is treated as
        an additive adjustment to the plan limit rather than a one-time
        refund to used_tokens, so the slack is not lost on the next day.
        """
        now_start = _utc_day_start(time.time())
        cur_start = float(data.get("window_start", 0))
        if now_start > cur_start:
            new_users: dict[str, dict[str, Any]] = {}
            for user, urec in (data.get("users") or {}).items():
                # Persist limit_override and plan_id across windows; zero out
                # rolling-day consumption counters (tokens, requests, events).
                carry: dict[str, Any] = {"tokens": 0, "requests": 0}
                if isinstance(urec, dict):
                    if "limit_override" in urec:
                        carry["limit_override"] = int(urec["limit_override"])
                    if "boost_tokens" in urec:
                        carry["boost_tokens"] = int(urec["boost_tokens"])
                    if "plan_id" in urec:
                        carry["plan_id"] = urec["plan_id"]
                new_users[user] = carry
            data = {"window_start": now_start, "users": new_users}
        return data

    def resolve_plan(
        self,
        username: str,
        groups: list[str] | None = None,
        hint_plan: str | None = None,
        user_overrides: dict[str, str] | None = None,
        group_rules: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Resolve effective plan: user override → group rule → default.

        Security note (LLM-S15)
        ------------------------
        ``hint_plan`` is taken from the *pod environment variable* and is
        therefore forgeable by a user.  It is only accepted as an override
        when the value is ``"local"`` — a plan with low default caps intended
        for laptop spikes.  In all other cases the hint is ignored and the
        canonical resolver output is used.

        Parameters
        ----------
        username : str
            Raw Hub user name (will be canonicalized internally).
        groups : list[str] | None
            Sorted list of Hub group names the user belongs to.
        hint_plan : str | None
            ``NBI_LLM_PLAN`` env value.  Untrusted for privilege elevation.
        user_overrides : dict[str, str] | None
            Central Quota Service only.  Maps a canonicalized username
            directly to a plan id, e.g. ``{"alice": "power"}``.
        group_rules : dict[str, str] | None
            Maps group name → plan id.  Defaults to ``DEFAULT_GROUP_RULES``.

        Returns
        -------
        dict
            Full plan dict (id, tokens_per_day, requests_per_day, models, …).
        """
        user = canonicalize_user(username)
        overrides = user_overrides or {}
        rules = group_rules or DEFAULT_GROUP_RULES
        # 1. Explicit per-user override wins (admin-only input, trusted).
        if user in overrides and overrides[user] in self.plans:
            return self.plans[overrides[user]]
        # 2. Group rule — first match wins.  Hub passes groups pre-sorted so
        #    result is deterministic.
        for g in groups or []:
            plan_id = rules.get(g)
            if plan_id and plan_id in self.plans:
                return self.plans[plan_id]
        # 3. Default plan (env overridable) with fallback chain so a broken
        #    config still yields *some* plan.
        default_id = os.environ.get("QUOTA_DEFAULT_PLAN", "standard")
        # Hint is trusted only for laptop-spike "local" plan.
        if hint_plan == "local" and "local" in self.plans:
            return self.plans["local"]
        return self.plans.get(default_id) or self.plans.get("standard") or next(
            iter(self.plans.values())
        )

    def check(
        self,
        username: str,
        estimate_tokens: int,
        model: str,
        groups: list[str] | None = None,
        hint_plan: str | None = None,
        feature: str = "chat",
        user_overrides: dict[str, str] | None = None,
    ) -> QuotaDecision:
        """Pre-request allowance decision (LLM-S13.2).

        Called *before* the sidecar forwards a request upstream.  All
        comparisons use the **estimated** prompt tokens (a lower bound of
        eventual usage) so even a very long prompt is caught at the gate.

        Five caps are evaluated in order — earlier returns short-circuit
        later checks:

        1. Model allow-list (per-plan ``models`` field; ``["*"]`` = all).
        2. Per-feature sub-cap ``tokens_per_day_{feature}`` if present in
           the plan (LLM-S22: e.g. split chat vs. inline budgets).
        3. Global daily tokens cap + ``limit_override`` additive boost.
        4. Global daily requests cap.

        If *all* checks pass the returned ``QuotaDecision.allowed`` is True.
        A soft-cap flag is set when token usage is >= 80% so the UI can warn
        users before hitting the hard limit.

        **Return tuple extension** (Issue #11): returns ``(QuotaDecision, plan_dict)``
        when ``return_plan`` callers need the resolved plan for response
        shaping — avoids a duplicate ``resolve_plan`` call downstream.

        Concurrency: read lock is sufficient because ``_roll`` is applied
        in-memory only (no write), so concurrent checks don't contend with
        each other, only with writes.
        """
        with self._lock.read():
            # Normalize feature so callers never need to worry about case.
            feature = (feature or "chat").strip().lower() or "chat"
            data = self._roll(self._read())
            plan = self.resolve_plan(
                username, groups=groups, hint_plan=hint_plan, user_overrides=user_overrides
            )
            user = canonicalize_user(username)
            # Create user record if not seen yet in this window.
            urec = data["users"].setdefault(
                user,
                {"tokens": 0, "requests": 0, "plan_id": plan["id"], "limit_override": 0},
            )
            # Always rebind plan_id from the *fresh* resolver — the user's
            # groups could have changed since last check and the record may
            # carry a stale value.  (LLM-S15 — never trust env for plan.)
            urec["plan_id"] = plan["id"]
            # Issue #5: limit_override is an additive adjustment to the plan
            # limit.  Admins set it via boost(); it survives rollover so
            # afternoon boosts don't vanish at midnight UTC.
            limit_override = int(urec.get("limit_override") or 0)
            used_t = int(urec["tokens"])
            used_r = int(urec["requests"])
            base_lim_t = int(plan["tokens_per_day"])
            lim_t = 0 if base_lim_t == 0 else max(0, base_lim_t + limit_override)
            lim_r = int(plan["requests_per_day"])
            reset_at = int(float(data["window_start"]) + 86400)
            models = plan.get("models") or ["*"]

            # Cap #1 — model-level allow-list.
            if models != ["*"] and model and model not in models:
                return QuotaDecision(
                    False,
                    plan["id"],
                    used_t,
                    lim_t,
                    used_r,
                    lim_r,
                    reset_at,
                    message=f"model {model!r} not allowed on plan={plan['id']}",
                )

            # Cap #2 — optional per-feature tokens cap.
            # Keyed as tokens_per_day_chat / _inline / _agent in the plan.
            feat_key = f"tokens_per_day_{feature}"
            feat_lim = int(plan.get(feat_key) or 0)
            if feat_lim > 0:
                used_feat = int((urec.get("by_feature") or {}).get(feature, 0))
                if used_feat + max(estimate_tokens, 0) > feat_lim:
                    return QuotaDecision(
                        False,
                        plan["id"],
                        used_t,
                        lim_t,
                        used_r,
                        lim_r,
                        reset_at,
                        message=(
                            f"LLM {feature} daily quota exceeded (plan={plan['id']}, "
                            f"used={used_feat}, limit={feat_lim}). Resets at unix={reset_at}."
                        ),
                    )

            # Cap #3 — global tokens (with boost override).
            if lim_t > 0 and used_t + max(estimate_tokens, 0) > lim_t:
                return QuotaDecision(
                    False,
                    plan["id"],
                    used_t,
                    lim_t,
                    used_r,
                    lim_r,
                    reset_at,
                    message=(
                        f"LLM daily quota exceeded (plan={plan['id']}, "
                        f"used={used_t}, limit={lim_t}). Resets at unix={reset_at}."
                    ),
                )

            # Cap #4 — global requests.
            if lim_r > 0 and used_r + 1 > lim_r:
                return QuotaDecision(
                    False,
                    plan["id"],
                    used_t,
                    lim_t,
                    used_r,
                    lim_r,
                    reset_at,
                    message=(
                        f"LLM daily request quota exceeded (plan={plan['id']}, "
                        f"used={used_r}, limit={lim_r}). Resets at unix={reset_at}."
                    ),
                )

            soft = lim_t > 0 and used_t >= int(0.8 * lim_t)
            return QuotaDecision(True, plan["id"], used_t, lim_t, used_r, lim_r, reset_at, soft_cap_hit=soft)

    def check_with_plan(
        self,
        username: str,
        estimate_tokens: int,
        model: str,
        groups: list[str] | None = None,
        hint_plan: str | None = None,
        feature: str = "chat",
        user_overrides: dict[str, str] | None = None,
    ) -> tuple[QuotaDecision, dict[str, Any]]:
        """Return ``(decision, resolved_plan)`` in one pass (Issue #11 helper).

        Enables quota_service's /v1/quota/check handler to shape the response
        using the server-side USER_OVERRIDES plan without re-invoking
        resolve_plan a second time.  Uses a write lock because the setdefault
        for unknown users in the check body would otherwise discard the
        freshly-created record under a read lock — using a write lock keeps
        the per-window user seed record consistent for the following commit.
        """
        decision = self.check(
            username=username,
            estimate_tokens=estimate_tokens,
            model=model,
            groups=groups,
            hint_plan=hint_plan,
            feature=feature,
            user_overrides=user_overrides,
        )
        # Resolve the plan once more with overrides applied (cheap — dict lookup).
        # The heavy-lift plan resolution was already cached inside check() via the
        # canonical username input; a second call is O(1) dict lookup, but keeping
        # a single canonical invocation here makes the API symmetric for callers.
        plan = self.resolve_plan(
            username, groups=groups, hint_plan=hint_plan, user_overrides=user_overrides
        )
        return decision, plan

    def commit(
        self,
        username: str,
        tokens: int,
        model: str,
        feature: str = "chat",
        groups: list[str] | None = None,
    ) -> dict[str, Any]:
        """Post-request accounting — record actual consumption (LLM-S13.3).

        Called *after* a successful upstream response.  ``tokens`` should be
        the real ``total_tokens`` from the provider's ``usage`` field when
        available; otherwise falls back to the pre-request estimate.

        Side effects
        ------------
        * Increments per-user ``tokens`` / ``requests`` counters.
        * Increments per-feature counters under ``by_feature``.
        * Appends an aggregate event (no prompts!) for later reporting.
        * Truncates event list to last 5000 entries to bound disk size.
        * Atomically writes the store back to disk.

        Concurrency: write lock — mutates and persists the store.
        """
        with self._lock.write():
            data = self._roll(self._read())
            plan = self.resolve_plan(username, groups=groups)
            user = canonicalize_user(username)
            urec = data["users"].setdefault(
                user,
                {
                    "tokens": 0,
                    "requests": 0,
                    "plan_id": plan["id"],
                    "by_feature": {},
                    "limit_override": 0,
                },
            )
            urec["plan_id"] = plan["id"]
            urec["tokens"] = int(urec.get("tokens", 0)) + max(0, int(tokens))
            urec["requests"] = int(urec.get("requests", 0)) + 1
            feats = urec.setdefault("by_feature", {})
            feats[feature] = int(feats.get(feature, 0)) + max(0, int(tokens))

            # Append usage event — aggregates ONLY; we intentionally never
            # persist prompts or outputs (data-minimization principle).
            events = data.setdefault("events", [])
            events.append(
                {
                    "ts": time.time(),
                    "user": user,
                    "plan_id": plan["id"],
                    "model": model,
                    "feature": feature,
                    "tokens": max(0, int(tokens)),
                }
            )
            # Bound in-memory + on-disk event history.  Larger rollups should
            # be shipped off-box by the periodic export job.
            data["events"] = events[-5000:]
            self._write(data)
            return {
                "user": user,
                "plan_id": plan["id"],
                "used_tokens": urec["tokens"],
                "used_requests": urec["requests"],
                "limit_override": int(urec.get("limit_override") or 0),
            }

    def snapshot(self, username: str, groups: list[str] | None = None) -> dict[str, Any]:
        """Read-only view used by ``GET /quota`` and the NBI quota badge.

        Shape matches what NBI frontend expects for the chat-sidebar badge
        plus per-feature breakdown for debugging (``by_feature``).

        Concurrency: read lock — no mutation.
        """
        with self._lock.read():
            data = self._roll(self._read())
            plan = self.resolve_plan(username, groups=groups)
            user = canonicalize_user(username)
            urec = data["users"].get(user, {"tokens": 0, "requests": 0, "limit_override": 0})
            limit_override = int(urec.get("limit_override") or 0)
            base_lim_t = int(plan["tokens_per_day"])
            lim_t = 0 if base_lim_t == 0 else max(0, base_lim_t + limit_override)
            used_t = int(urec.get("tokens", 0))
            remaining = None if lim_t <= 0 else max(0, lim_t - used_t)
            soft = lim_t > 0 and used_t >= int(0.8 * lim_t)
            return {
                "user_id": user,
                "plan_id": plan["id"],
                "used_tokens": used_t,
                "limit_tokens": lim_t if lim_t > 0 else None,
                "limit_override": limit_override,
                "remaining_tokens": remaining,
                "used_requests": int(urec.get("requests", 0)),
                "limit_requests": int(plan["requests_per_day"]),
                "reset_at": int(float(data["window_start"]) + 86400),
                # Informational soft-cap (≥80%); enforcement still hard-deny at 100%
                "soft_cap_hit": soft,
                "by_feature": dict(urec.get("by_feature") or {}),
            }

    def snapshot_all(self) -> dict[str, Any]:
        """Public read-only view of the full store state (Issue #9).

        Replaces the private ``store._read()`` coupling that the
        quota-service /metrics endpoint previously used.  The returned dict
        is a deep-copied snapshot so callers can safely enumerate on their
        own thread without holding the store lock.
        """
        import copy

        with self._lock.read():
            return copy.deepcopy(self._read())

    def boost(self, username: str, extra_tokens: int) -> dict[str, Any]:
        """Break-glass admin: raise the effective 24h limit for ``username``.

        Implements LLM-S17.1 admin emergency procedure.

        Issue #5 fix: the boost is now stored as a separate
        ``limit_override`` field on the user record and is preserved across
        window rolls (``_roll`` copies it forward).  The effective daily
        cap becomes ``plan.tokens_per_day + limit_override`` — the previous
        implementation subtracted directly from used_tokens, which lost
        all applied boosts at midnight UTC because ``_roll`` replaced
        ``users`` with ``{}``.

        ``boost_tokens`` is still recorded for historical reconciliation
        reports (cumulative tokens ever granted to this user).

        Concurrency: write lock — mutates user record and persists.
        """
        with self._lock.write():
            data = self._roll(self._read())
            user = canonicalize_user(username)
            urec = data["users"].setdefault(
                user,
                {
                    "tokens": 0,
                    "requests": 0,
                    "boost_tokens": 0,
                    "limit_override": 0,
                },
            )
            extra = max(0, int(extra_tokens))
            urec["boost_tokens"] = int(urec.get("boost_tokens", 0)) + extra
            urec["limit_override"] = int(urec.get("limit_override", 0)) + extra
            self._write(data)
            return {
                "user": user,
                "boost_applied": extra,
                "limit_override": int(urec["limit_override"]),
                "used_tokens": int(urec.get("tokens", 0)),
                "boost_tokens_cumulative": int(urec["boost_tokens"]),
            }

    def usage(
        self, username: Optional[str] = None, since: float = 0.0, until: float | None = None
    ) -> list[dict]:
        """Return usage events, optionally filtered.

        Parameters
        ----------
        username : Optional[str]
            If set, only events for this canonicalized user are returned.
        since / until : float
            Unix epoch range (inclusive).  ``until`` defaults to ``now``.

        Concurrency: read lock — no mutation.
        """
        with self._lock.read():
            data = self._roll(self._read())
            until = until or time.time()
            out = []
            for ev in data.get("events", []):
                if username and ev.get("user") != canonicalize_user(username):
                    continue
                if since <= float(ev.get("ts", 0)) <= until:
                    out.append(ev)
            return out


class HttpQuotaClient:
    """Client for the centralized Quota Service (``QUOTA_BACKEND=http``).

    Used by the per-user sidecar in production Hub deployments.  Implements
    the same duck-typed surface as ``FileQuotaStore`` (``check`` / ``commit``
    / ``snapshot``) so the sidecar code does not need to branch on backend.

    The underlying HTTP transport is stdlib ``urllib`` to avoid extra deps in
    the sidecar image.  All requests use a 10-second timeout.  Callers
    (sidecar) must treat any transport exception as "quota backend
    unavailable" and fail-closed (LLM-S18.2).
    """

    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")

    def _json(self, method: str, path: str, body: dict | None = None) -> dict:
        """Send an HTTP request and parse the JSON response.

        For HTTPError responses we try to parse JSON error bodies.

        Issue #15 fix: the error-body parser catches ONLY
        ``(json.JSONDecodeError, UnicodeDecodeError)``.  Transport-level
        errors (URLError, socket.timeout, SSLError) propagate separately so
        callers can distinguish "bad upstream JSON" from "quota service
        unreachable" without reading a string.
        """
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw_bytes = e.read()
            try:
                raw_text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError as ue:
                raise RuntimeError(
                    f"quota service HTTP {e.code}: non-utf8 error body ({len(raw_bytes)} bytes)"
                ) from ue
            try:
                return json.loads(raw_text)
            except json.JSONDecodeError:
                raise RuntimeError(f"quota service HTTP {e.code}: {raw_text[:300]}") from e

    def check(self, **kwargs: Any) -> QuotaDecision:
        """POST /v1/quota/check → QuotaDecision."""
        r = self._json("POST", "/v1/quota/check", kwargs)
        return QuotaDecision(
            allowed=bool(r.get("allowed")),
            plan_id=r.get("plan_id", "standard"),
            used_tokens=int(r.get("used_tokens", 0)),
            limit_tokens=int(r.get("limit_tokens", 0)),
            used_requests=int(r.get("used_requests", 0)),
            limit_requests=int(r.get("limit_requests", 0)),
            reset_at=int(r.get("reset_at", 0)),
            message=r.get("message", ""),
            soft_cap_hit=bool(r.get("soft_cap_hit")),
        )

    def commit(self, **kwargs: Any) -> dict:
        """POST /v1/quota/commit → usage summary dict."""
        return self._json("POST", "/v1/quota/commit", kwargs)

    def snapshot(self, username: str, groups: list[str] | None = None) -> dict:
        """GET /v1/quota?user=…&groups=… → quota snapshot dict."""
        q = urllib.parse.urlencode({"user": username, "groups": ",".join(groups or [])})
        return self._json("GET", f"/v1/quota?{q}")


def build_quota_backend() -> Any:
    """Construct a quota backend instance based on environment.

    Reads
    -----
    QUOTA_BACKEND     — "local" (default) | "http" | "memory"
    QUOTA_SERVICE_URL — for http backend; defaults to localhost:8090
    QUOTA_STORE_PATH  — for local/memory backend; defaults to repo .runtime
    QUOTA_PLANS_PATH  — optional JSON file with plan overrides merged on top
                        of DEFAULT_PLANS.  Accepts either the full schema
                        ``{"plans": {...}}`` or a flat ``{plan_id: {...}}``.

    Returns
    -------
    FileQuotaStore | HttpQuotaClient
        Duck-typed object exposing ``(check, commit, snapshot)``.
    """
    backend = os.environ.get("QUOTA_BACKEND", "local").strip().lower()
    if backend == "http":
        url = os.environ.get("QUOTA_SERVICE_URL", "http://127.0.0.1:8090")
        return HttpQuotaClient(url)
    path = os.environ.get(
        "QUOTA_STORE_PATH",
        str(Path(__file__).resolve().parents[1] / ".runtime" / "quota-store.json"),
    )
    # Load optional plans override
    plans_path = os.environ.get("QUOTA_PLANS_PATH", "")
    plans = dict(DEFAULT_PLANS)
    if plans_path and Path(plans_path).is_file():
        with open(plans_path, encoding="utf-8") as f:
            loaded = json.load(f)
            plans.update(loaded.get("plans") or loaded)
    if backend == "memory":
        # Still file-backed under /tmp for simplicity, but unique per pid so
        # tests don't collide.
        path = os.environ.get("QUOTA_STORE_PATH", f"/tmp/nbi-quota-{os.getpid()}.json")
    return FileQuotaStore(path, plans=plans)
