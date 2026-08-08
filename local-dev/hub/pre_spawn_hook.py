"""JupyterHub pre_spawn_hook: bind Hub login → NBI_LLM_* env (LLM-S11.3).

Usage in jupyterhub_config.py:

    from pre_spawn_hook import make_pre_spawn_hook
    c.KubeSpawner.pre_spawn_hook = make_pre_spawn_hook(
        quota_service_url="http://quota-service.jhub.svc:8090",
    )
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any, Callable, Optional

log = logging.getLogger("nbi.pre_spawn")


def canonicalize_user(username: str) -> str:
    u = (username or "").strip().lower()
    if "@" in u:
        u = u.split("@", 1)[0]
    return u


def resolve_plan_via_http(
    base_url: str, username: str, groups: list[str], auth_state: dict
) -> dict[str, Any]:
    body = json.dumps(
        {"username": username, "groups": groups, "auth_state_keys": list(auth_state.keys())}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/plans/resolve",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def resolve_plan_local(username: str, groups: list[str]) -> dict[str, Any]:
    """Fallback when Quota Service is unreachable (still inject identity)."""
    rules = {"interns": "intern", "ml-platform": "power"}
    for g in groups:
        if g in rules:
            pid = rules[g]
            break
    else:
        pid = "standard"
    catalog = {
        "intern": {"id": "intern", "tokens_per_day": 200000, "requests_per_day": 200},
        "standard": {"id": "standard", "tokens_per_day": 2000000, "requests_per_day": 2000},
        "power": {"id": "power", "tokens_per_day": 20000000, "requests_per_day": 20000},
    }
    return catalog[pid]


def make_pre_spawn_hook(
    quota_service_url: Optional[str] = None,
) -> Callable[[Any], None]:
    async def pre_spawn_hook(spawner: Any) -> None:
        auth_state = {}
        try:
            auth_state = (await spawner.user.get_auth_state()) or {}
        except Exception:  # noqa: BLE001
            auth_state = {}
        groups: list[str] = []
        try:
            groups = sorted(g.name for g in spawner.user.groups)
        except Exception:  # noqa: BLE001
            groups = []
        username = canonicalize_user(spawner.user.name)
        plan: dict[str, Any]
        if quota_service_url:
            try:
                plan = resolve_plan_via_http(quota_service_url, username, groups, auth_state)
            except Exception as exc:  # noqa: BLE001
                log.warning("quota resolve failed (%s); using local fallback", exc)
                plan = resolve_plan_local(username, groups)
        else:
            plan = resolve_plan_local(username, groups)

        env = {
            "NBI_LLM_USER": username,
            "NBI_LLM_GROUPS": ",".join(groups),
            "NBI_LLM_PLAN": plan.get("id", "standard"),
            "NBI_LLM_QUOTA_TOKENS_DAY": str(plan.get("tokens_per_day", "")),
            "NBI_LLM_QUOTA_REQ_DAY": str(plan.get("requests_per_day", "")),
            "TOKEN_PROVIDER": "mock",  # override to jar in prod image
            "QUOTA_BACKEND": "http" if quota_service_url else "local",
        }
        if quota_service_url:
            env["QUOTA_SERVICE_URL"] = quota_service_url
        spawner.environment.update(env)
        log.info(
            "injected NBI_LLM_* for user=%s plan=%s groups=%s",
            username,
            plan.get("id"),
            groups,
        )

    return pre_spawn_hook
