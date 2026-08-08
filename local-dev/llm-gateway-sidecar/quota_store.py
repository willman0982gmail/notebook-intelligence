#!/usr/bin/env python3
"""Quota check/commit client + durable file store (LLM-S12/S13/S15).

Modes (QUOTA_BACKEND):
  local   — file-backed counters under QUOTA_STORE_PATH (default)
  http    — call Quota Service at QUOTA_SERVICE_URL
  memory  — process-local only (tests)
"""

from __future__ import annotations

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
from typing import Any, Optional

log = logging.getLogger("sidecar.quota")

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
    "local": {
        "id": "local",
        "tokens_per_day": int(os.environ.get("QUOTA_TOKENS_DAY", "50000")),
        "requests_per_day": int(os.environ.get("QUOTA_REQ_DAY", "10000")),
        "models": ["*"],
    },
}

# group name -> plan id
DEFAULT_GROUP_RULES: dict[str, str] = {
    "interns": "intern",
    "ml-platform": "power",
}


def canonicalize_user(username: str) -> str:
    """Single subject key rule (LLM-S11.4): lowercase; strip email domain if present."""
    u = (username or "").strip().lower()
    if "@" in u:
        u = u.split("@", 1)[0]
    return u or "anonymous"


@dataclass
class QuotaDecision:
    allowed: bool
    plan_id: str
    used_tokens: int
    limit_tokens: int
    used_requests: int
    limit_requests: int
    reset_at: int
    message: str = ""
    soft_cap_hit: bool = False


class FileQuotaStore:
    """Durable JSON counters surviving process restart (LLM-S13.1/S13.6)."""

    def __init__(self, path: str | Path, plans: dict | None = None) -> None:
        self.path = Path(path)
        self.plans = plans or dict(DEFAULT_PLANS)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"window_start": time.time(), "users": {}})

    def _read(self) -> dict:
        with self.path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(self.path)

    def _roll(self, data: dict) -> dict:
        if time.time() - float(data.get("window_start", 0)) >= 86400:
            data = {"window_start": time.time(), "users": {}}
        return data

    def resolve_plan(
        self,
        username: str,
        groups: list[str] | None = None,
        hint_plan: str | None = None,
        user_overrides: dict[str, str] | None = None,
        group_rules: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Resolve plan: user override → group → default.

        ``hint_plan`` from the pod env is **not** trusted to raise privileges.
        Only ``local`` is accepted as a non-production default for laptop spikes.
        """
        user = canonicalize_user(username)
        overrides = user_overrides or {}
        rules = group_rules or DEFAULT_GROUP_RULES
        if user in overrides and overrides[user] in self.plans:
            return self.plans[overrides[user]]
        for g in groups or []:
            plan_id = rules.get(g)
            if plan_id and plan_id in self.plans:
                return self.plans[plan_id]
        default_id = os.environ.get("QUOTA_DEFAULT_PLAN", "standard")
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
    ) -> QuotaDecision:
        with self._lock:
            feature = (feature or "chat").strip().lower() or "chat"
            data = self._roll(self._read())
            plan = self.resolve_plan(username, groups=groups, hint_plan=hint_plan)
            user = canonicalize_user(username)
            urec = data["users"].setdefault(
                user, {"tokens": 0, "requests": 0, "plan_id": plan["id"]}
            )
            # Always re-bind plan from resolver (S15 — ignore forged env plan).
            urec["plan_id"] = plan["id"]
            used_t = int(urec["tokens"])
            used_r = int(urec["requests"])
            lim_t = int(plan["tokens_per_day"])
            lim_r = int(plan["requests_per_day"])
            reset_at = int(float(data["window_start"]) + 86400)
            models = plan.get("models") or ["*"]
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
            # Optional per-feature caps (LLM-S22): tokens_per_day_chat / _inline
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

    def commit(
        self,
        username: str,
        tokens: int,
        model: str,
        feature: str = "chat",
        groups: list[str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            data = self._roll(self._read())
            plan = self.resolve_plan(username, groups=groups)
            user = canonicalize_user(username)
            urec = data["users"].setdefault(
                user, {"tokens": 0, "requests": 0, "plan_id": plan["id"], "by_feature": {}}
            )
            urec["plan_id"] = plan["id"]
            urec["tokens"] = int(urec.get("tokens", 0)) + max(0, int(tokens))
            urec["requests"] = int(urec.get("requests", 0)) + 1
            feats = urec.setdefault("by_feature", {})
            feats[feature] = int(feats.get(feature, 0)) + max(0, int(tokens))
            # usage events: aggregates only — never prompts
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
            # keep last 5000 events
            data["events"] = events[-5000:]
            self._write(data)
            return {
                "user": user,
                "plan_id": plan["id"],
                "used_tokens": urec["tokens"],
                "used_requests": urec["requests"],
            }

    def snapshot(self, username: str, groups: list[str] | None = None) -> dict[str, Any]:
        with self._lock:
            data = self._roll(self._read())
            plan = self.resolve_plan(username, groups=groups)
            user = canonicalize_user(username)
            urec = data["users"].get(user, {"tokens": 0, "requests": 0})
            lim_t = int(plan["tokens_per_day"])
            used_t = int(urec.get("tokens", 0))
            remaining = None if lim_t <= 0 else max(0, lim_t - used_t)
            soft = lim_t > 0 and used_t >= int(0.8 * lim_t)
            return {
                "user_id": user,
                "plan_id": plan["id"],
                "used_tokens": used_t,
                "limit_tokens": lim_t if lim_t > 0 else None,
                "remaining_tokens": remaining,
                "used_requests": int(urec.get("requests", 0)),
                "limit_requests": int(plan["requests_per_day"]),
                "reset_at": int(float(data["window_start"]) + 86400),
                # Informational soft-cap (≥80%); enforcement still hard-deny at 100%
                "soft_cap_hit": soft,
                "by_feature": dict(urec.get("by_feature") or {}),
            }

    def boost(self, username: str, extra_tokens: int) -> dict[str, Any]:
        """Break-glass: temporarily raise effective limit by lowering used (or store boost)."""
        with self._lock:
            data = self._roll(self._read())
            user = canonicalize_user(username)
            urec = data["users"].setdefault(user, {"tokens": 0, "requests": 0, "boost_tokens": 0})
            urec["boost_tokens"] = int(urec.get("boost_tokens", 0)) + max(0, extra_tokens)
            # Apply boost as negative used for simplicity in local MVP
            urec["tokens"] = max(0, int(urec.get("tokens", 0)) - max(0, extra_tokens))
            self._write(data)
            return {"user": user, "boost_applied": extra_tokens, "used_tokens": urec["tokens"]}

    def usage(
        self, username: Optional[str] = None, since: float = 0.0, until: float | None = None
    ) -> list[dict]:
        with self._lock:
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
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")

    def _json(self, method: str, path: str, body: dict | None = None) -> dict:
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
            raw = e.read().decode("utf-8")
            try:
                return json.loads(raw)
            except Exception:  # noqa: BLE001
                raise RuntimeError(f"quota service HTTP {e.code}: {raw[:300]}") from e

    def check(self, **kwargs: Any) -> QuotaDecision:
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
        return self._json("POST", "/v1/quota/commit", kwargs)

    def snapshot(self, username: str, groups: list[str] | None = None) -> dict:
        q = urllib.parse.urlencode({"user": username, "groups": ",".join(groups or [])})
        return self._json("GET", f"/v1/quota?{q}")


def build_quota_backend() -> Any:
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
        # still file under tmp for simplicity — use unique path
        path = os.environ.get("QUOTA_STORE_PATH", f"/tmp/nbi-quota-{os.getpid()}.json")
    return FileQuotaStore(path, plans=plans)
